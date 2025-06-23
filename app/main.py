from fastapi import FastAPI
from app.api.routes import router as api_router

app = FastAPI(title="Guacamayo Marketing API")

app.include_router(api_router)

@app.get("/")
async def read_root():
    return {"message": "API de Guacamayo Marketing está funcionando"}