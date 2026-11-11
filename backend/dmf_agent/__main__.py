"""命令行入口：python -m dmf_agent"""

import uvicorn

from dmf_agent.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "dmf_agent.api.app:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.debug,
    )


if __name__ == "__main__":
    main()
