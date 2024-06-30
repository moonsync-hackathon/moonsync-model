from modal import (
    App,
    Image,
    build,
    enter,
    web_endpoint,
    Secret,
    Volume,
)

from os import environ

from fastapi import Request
from fastapi.responses import StreamingResponse
from typing import Dict

from config.base import (
    MODAL_CPU,
    MODAL_MEMORY,
    MODAL_CONTAINER_IDLE_TIMEOUT,
    OPENAI_MODEL,
    OPENAI_MODEL_TEMPERATURE,
    PPLX_MODEL,
    PPLX_MODEL_TEMPERATURE,
    WEATHER_API_URL,
)

from config.prompts import (
    SOURCE_QA_PROMPT_SYSTEM,
    SOURCE_QA_PROMPT_USER,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_ENTIRE_CHAT,
    SUB_QUESTION_PROMPT_TMPL,
    REFINE_SYSTEM_PROMPT,
    REFINE_USER_PROMPT,
    FORWARD_PROMPT,
)

from models import (
    LLMModel,
    VectorDB,
    QueryEngine,
)

from handlers import ChatHandler, EventSchedulerHandler, ImageHandler, DashBoardHandler

from utils.biometrics import get_onboarding_data, get_dashboard_data

moonsync_image = Image.debian_slim(python_version="3.10").pip_install(
    "arize-phoenix[evals]~=3.22.0",
    "gcsfs~=2024.3.1",
    "llama-index-core~=0.10.29",
    "llama-index-agent-openai~=0.2.2",
    "llama-index-callbacks-arize-phoenix~=0.1.2",
    "llama-index-experimental~=0.1.3",
    "llama-index-llms-anthropic~=0.1.10",
    "llama-index-llms-openai-like~=0.1.3",
    "llama-index-vector-stores-pinecone~=0.1.4",
    "llama-index~=0.10.29",
    "nomic~=3.0.21",
    "openinference-instrumentation-llama-index~=1.2.0",
    "pinecone-client~=3.2.2",
    "requests~=2.31.0",
    "fastapi~=0.68.1",
    "pandas~=2.2.1",
    "terra-python~=0.0.12",
    "llama-index-llms-perplexity~=0.1.3",
    "llama-index-question-gen-guidance~=0.1.2",
    "llama-index-tools-google",
    "llama-index-multi-modal-llms-openai",
    "llama-index-llms-azure-openai",
    "llama-index-multi-modal-llms-azure-openai",
    "langfuse",
    "llama-index-llms-groq",
    "llama-index-embeddings-azure-openai",
    "supabase",
    "psycopg2-binary",
)

moonsync_volume = Volume.from_name("moonsync")

app = App("moonsync-modal-app")


@app.cls(
    cpu=MODAL_CPU,
    memory=MODAL_MEMORY,
    container_idle_timeout=MODAL_CONTAINER_IDLE_TIMEOUT,
    image=moonsync_image,
    secrets=[Secret.from_name("moonsync-secret")],
    volumes={"/volumes/moonsync": moonsync_volume},
    keep_warm=1,
)
class Model:
    @build()
    def build(self):
        pass

    @enter()
    def enter(self):
        import os
        import shutil
        from llama_index.core import Settings
        from llama_index.vector_stores.pinecone import PineconeVectorStore
        from llama_index.core.prompts import (
            ChatPromptTemplate,
        )

        from llama_index.core.llms import ChatMessage, MessageRole
        from datetime import datetime
        from llama_index.core.callbacks import CallbackManager
        from langfuse.llama_index import LlamaIndexCallbackHandler

        # SUPABASE SETUP
        from supabase import create_client, Client

        self.SYSTEM_PROMPT = SYSTEM_PROMPT

        url: str = os.environ["SUPABASE_URL"]
        key: str = os.environ["SUPABASE_KEY"]

        # TODO: see the use case for this
        self.supabase: Client = create_client(url, key)

        self.groq = LLMModel.get_groq_model(
            model="llama3-8b-8192", api_key=environ["GROQ_API_KEY"]
        )
        self.llm = LLMModel.get_openai_model(
            model="gpt-4-turbo", api_key=environ["OPENAI_API_KEY"]
        )

        self.subquestion_llm = LLMModel.get_openai_like_model(
            model="llama3-8b-8192",
            api_base="https://api.groq.com/openai/v1",
            api_key=environ["GROQ_API_KEY"],
            temperature=0.1,
            is_function_calling_model=True,
            is_chat_model=True,
        )

        self.embed_model = LLMModel.get_azure_embedding_model(
            model="text-embedding-ada-002",
            deployment_name="embedding-model",
            api_key=environ["AZURE_CHAT_API_KEY"],
            azure_endpoint=environ["AZURE_CHAT_ENDPOINT"],
            api_version="2023-10-01-preview",
        )

        self.pplx_llm = LLMModel.get_perplexity_model(
            model=PPLX_MODEL,
            api_key=environ["PPLX_API_KEY"],
            temperature=PPLX_MODEL_TEMPERATURE,
        )

        langfuse_callback_handler = LlamaIndexCallbackHandler()
        Settings.callback_manager = CallbackManager([langfuse_callback_handler])

        # Init Pinecone
        api_key = os.environ["PINECONE_API_KEY"]
        vector_db = VectorDB(api_key=api_key)

        Settings.llm = self.llm
        Settings.embed_model = self.embed_model

        # TERRA ENVIRONMENT VARIABLES
        self.TERRA_DEV_ID = os.environ["TERRA_DEV_ID"]
        self.TERRA_API_KEY = os.environ["TERRA_API_KEY"]

        # setup token.json for gcal
        token_json = "/volumes/moonsync/google_credentials/token.json"
        destination_path = "token.json"
        shutil.copy(token_json, destination_path)

        # Pincone Indexes
        vector_indexes = vector_db.get_vector_indexes(
            [
                "moonsync-index-mood-feeling",
                "moonsync-index-general",
                "moonsync-index-diet-nutrition",
                "moonsync-index-fitness-wellness",
            ]
        )

        # Update prompt to include sources
        sources_qa_prompt = [
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=(SOURCE_QA_PROMPT_SYSTEM),
            ),
            ChatMessage(
                role=MessageRole.USER,
                content=(SOURCE_QA_PROMPT_USER),
            ),
        ]
        sources_prompt = ChatPromptTemplate(sources_qa_prompt)

        # Create Query Engines

        query_engines = QueryEngine.get_vector_query_engines(
            vector_indexes=vector_indexes,
            llm=self.groq,
            similarity_top_k=2,
            text_qa_template=sources_prompt,
        )
        dashboard_data_query_engines = QueryEngine.get_dashboard_query_engines(
            vector_indexes=vector_indexes,
            llm=self.groq,
            similarity_top_k=2,
        )
        (
            mood_feeling_query_engine,
            general_query_engine,
            diet_nutrition_query_engine,
            fitness_wellness_query_engine,
        ) = query_engines

        self.mood_feeling_qe, _, self.diet_nutrition_qe, self.fitness_wellness_qe = (
            dashboard_data_query_engines
        )

        empty_query_engine = QueryEngine.get_empty_query_engine()

        # SQL Query Engine
        db_url = environ["SUPABASE_DB_URL"]
        sql_query_engine = QueryEngine.get_sql_query_engine(
            db_url=db_url,
            tables=["user_biometrics"],
            llm=self.llm,
        )

        # Text QA Prompt
        # Get the current date
        # TODO: refactor this
        self.current_date = datetime.strptime(
            datetime.today().strftime("%Y-%m-%d"), "%Y-%m-%d"
        ).date()
        print("Current date: ", self.current_date)
        day_of_week = self.current_date.weekday()
        day_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        self.day_name = day_names[day_of_week]
        # TODO: change this to get user specific data - fix self.df.iloc[-1]['menstrual_phase']
        self.content_template = f"\nImportant information to be considered while answering the query:\nCurrent Mensural Phase: Follicular \nToday's date: {self.current_date} \nDay of the week: {self.day_name} \n Current Location: New York City"
        self.phase_info = f"My current mensural phase is: Follicular"

        tools_data = [
            {
                "query_engine": mood_feeling_query_engine,
                "name": "mood/feeling",
                "description": "Useful for questions related to women's mood and feelings",
            },
            {
                "query_engine": diet_nutrition_query_engine,
                "name": "diet/nutrition",
                "description": "Useful for questions related to women's diet and nutrition recommendatations",
            },
            {
                "query_engine": general_query_engine,
                "name": "general",
                "description": "Useful for general questions related to women's menstrual cycle",
            },
            {
                "query_engine": fitness_wellness_query_engine,
                "name": "fitness/wellness",
                "description": "Useful for questions related to fitness and wellness advice for women",
            },
            {
                "query_engine": empty_query_engine,
                "name": "NOTA",
                "description": "Use this if none of the other tools are relevant to the query",
            },
            {
                "query_engine": sql_query_engine,
                "name": "database",
                "description": """Use this to get relevant biometrics (health parameters) data relevant to the query from the 'user_biometrics' SQL table.
            Always use the terra_user_id to filter data for the given user. You have access to the following columns - 
            id, avg_hr_bpm, resting_hr_bpm, duration_in_bed_seconds_data, duration_deep_sleep, temperature_delta, avg_saturation_percentage, recovery_score, activity_score, sleep_score, stress_data, number_steps, total_burned_calories, date, terra_user_id
            """,
            },
        ]

        query_engine_tools = QueryEngine.get_query_engine_tools(tools_data)

        self.sub_question_query_engine = QueryEngine.get_subquestion_query_engine(
            query_engine_tools=query_engine_tools, subquestion_llm=self.subquestion_llm
        )

        self.chat_engine = QueryEngine.get_chat_engine(
            query_engine=self.sub_question_query_engine,
            user_info_content=self.content_template,
        )

    def _inference(self, prompt: str, messages):
        chat_handler = ChatHandler(
            prompt=prompt,
            messages=messages,
            user_info_content=self.content_template,
            sub_question_query_engine=self.sub_question_query_engine,
            chat_engine=self.chat_engine,
            menstrual_phase_info=self.phase_info,
        )
        self.chat_engine, streaming_response = chat_handler.run_offline()
        for token in streaming_response.response_gen:
            yield token

    def _online_inference(self, prompt: str, messages):
        chat_handler = ChatHandler(
            prompt=prompt,
            messages=messages,
            user_info_content=self.content_template,
        )
        curr_history = chat_handler.run_online()
        resp = self.pplx_llm.stream_chat(curr_history)
        for r in resp:
            yield r.delta

    # Event schedule runner
    def _event_schedule_runner(self, prompt: str, messages):
        event_scheduler_handler = EventSchedulerHandler(
            prompt=prompt,
            messages=messages,
            llm=self.llm,  # has to am OpenAI model
            current_date=self.current_date,
        )
        response_gen = event_scheduler_handler.run()
        for token in response_gen:
            yield token

    @web_endpoint(method="POST")
    def web_inference(self, request: Request, item: Dict):
        prompt, image_url, image_response = "", "", ""
        if isinstance(item["prompt"], list):
            for value in item["prompt"]:
                if value["type"] == "text":
                    prompt = value["text"]
                if value["type"] == "image_url":
                    image_url = value["image_url"]["url"]
        else:
            prompt = item["prompt"]

        messages = item["messages"]
        if image_url:
            image_response = ImageHandler(image_url=image_url).run()

        # Get the headers
        city = request.headers.get("x-vercel-ip-city", "NYC")
        region = request.headers.get("x-vercel-ip-country-region", "New York")
        country = request.headers.get("x-vercel-ip-country", "USA")

        # Get user terra id
        terra_user_id = item.get("terra_user_id", None)
        if terra_user_id:
            prompt = prompt + f"\nTerra User ID: {terra_user_id}"
        print(f"City: {city}, Region: {region}, Country: {country}")

        if "@internet" in prompt:
            return StreamingResponse(
                self._online_inference(prompt=prompt, messages=messages),
                media_type="text/event-stream",
            )

        if "schedule" in prompt:
            return StreamingResponse(
                self._event_schedule_runner(prompt=prompt, messages=messages),
                media_type="text/event-stream",
            )

        if image_response != "":
            prompt = (
                prompt
                + "\n"
                + "Additional information about the image uploaded \n "
                + image_response.text
            )

        return StreamingResponse(
            self._inference(prompt=prompt, messages=messages),
            media_type="text/event-stream",
        )

    @web_endpoint(method="POST", label="dashboard")
    def dashboard_details(self):

        return DashBoardHandler(
            mood_feeling_qe=self.mood_feeling_qe,
            diet_nutrition_qe=self.diet_nutrition_qe,
            fitness_wellness_qe=self.fitness_wellness_qe,
        ).run()

    def _get_weather(self):
        import requests
        import os

        api_key = os.environ["WEATHER_API_KEY"]
        base_url = WEATHER_API_URL

        params = {"key": api_key, "q": "New York City", "aqi": "no"}

        response = requests.get(base_url, params=params)

        if response.status_code == 200:
            data = response.json()

            location = data["location"]["name"]
            temp_f = data["current"]["temp_f"]
            condition = data["current"]["condition"]["text"]
            print(f"Location: {location}")
            print(f"Condition: {condition}")
            print(f"Current temperature: {temp_f}°F")
        else:
            print("Error fetching weather data")

        return {"location": location, "condition": condition, "temp_f": temp_f}

    @web_endpoint(method="POST", label="biometrics")
    def biometrics_details(self):
        # TODO read user id from body

        menstrual_phase = self.df.iloc[-1]["menstrual_phase"]
        sleep = self.df.iloc[-1]["duration_in_bed"]
        temperature = 98.6 + self.df.iloc[-1]["temperature_delta"]

        m, s = divmod(sleep, 60)
        hours, mins = divmod(m, 60)

        sleep = f"{hours} hours {mins} mins"

        weather_data = self._get_weather()

        response_json = {
            "menstrual_phase": menstrual_phase,
            "sleep": sleep,
            "body_temperature": round(temperature, 2),
        }
        response_json.update(weather_data)
        return response_json

    @web_endpoint(method="POST", label="init-biometrics")
    def initial_biometric_data_load(self, request: Request, item: Dict):
        user_id = item["user_id"]

        # Get the biometric data
        get_onboarding_data(
            self.TERRA_DEV_ID, self.TERRA_API_KEY, user_id, self.supabase
        )

        response_json = {"status": "complete"}

        return response_json

    @web_endpoint(method="POST", label="dashboard-biometrics")
    def dasboard_biometric_data_load(self, request: Request, item: Dict):
        import os

        user_id = item["user_id"]
        DEV_ID = os.environ["TERRA_DEV_ID"]
        API_KEY = os.environ["TERRA_API_KEY"]

        # TODO - Update menstrual phase
        menstrual_phase = "Follicular"

        sleep, temperature = get_dashboard_data(DEV_ID, API_KEY, user_id)
        print("[DASHBOARD DATA]", sleep, temperature)
        m, _ = divmod(sleep, 60)
        hours, mins = divmod(m, 60)

        sleep = f"{hours} hours {mins} mins"

        weather_data = self._get_weather()

        response_json = {
            "status": "complete",
            "sleep": sleep,
            "body_temperature": round(temperature if temperature else 0 + 98.6, 2),
            "menstrual_phase": menstrual_phase,
        }

        response_json.update(weather_data)

        return response_json
