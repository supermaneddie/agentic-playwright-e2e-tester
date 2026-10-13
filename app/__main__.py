import uvicorn

from app.config import load_config


def main():
    settings = load_config()
    uvicorn.run("app.main:app", host=settings.server.host, port=settings.server.port, workers=1)


if __name__ == "__main__":
    main()
