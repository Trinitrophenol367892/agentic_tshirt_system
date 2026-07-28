import time
from src.database import init_db
from src.orchestrator import run_pipeline
from src.logger import setup_logger

log_main = setup_logger("main")

if __name__ == "__main__":
    log_main.info("Agentic T-Shirt System - Starting...")
    log_main.debug("Python entry point: main.py")

    start = time.time()
    init_db()
    run_pipeline()

    log_main.info("Total execution time: %.2fs", time.time() - start)
    log_main.info("Shutdown complete.")
