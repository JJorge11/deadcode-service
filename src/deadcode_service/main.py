from fastapi import FastAPI

app = FastAPI(title="deadcode-service")


@app.get("/health")
def health():
    return {"status": "ok"}