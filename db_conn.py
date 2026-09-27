import os
from dotenv import load_dotenv
import sqlalchemy
from sqlalchemy.engine import URL
load_dotenv()
def conn_db_wf():
    db_url = URL.create(
        drivername="postgresql+psycopg",
        username=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host="localhost",
        port=os.getenv("PORT"),
        database=os.getenv("POSTGRES_DB"),
    )
    engine = sqlalchemy.create_engine(db_url)
    metadata = sqlalchemy.MetaData()
    metadata.reflect(bind=engine)
    return engine, metadata