import logging
import sys


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        record.name = f"{self.BOLD}{record.name}{self.RESET}"
        return super().format(record)


def setup_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = ColorFormatter(
            fmt="%(asctime)s | %(levelname)s | %(name)-22s | %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


log_db = setup_logger("database")
log_llm = setup_logger("llm_client")
log_ingest = setup_logger("ingest")
log_analysis = setup_logger("analysis")
log_generator = setup_logger("generator")
log_judge = setup_logger("judge")
log_approval = setup_logger("approval")
log_publisher = setup_logger("publisher")
log_traffic = setup_logger("traffic")
log_orchestrator = setup_logger("orchestrator")
