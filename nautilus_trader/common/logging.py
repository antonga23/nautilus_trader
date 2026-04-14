from __future__ import annotations

from typing import Any

from nautilus_trader.common.component import Logger as _ComponentLogger


class Logger:
    """
    Backwards-compatible wrapper for older adapter code which still imports
    `nautilus_trader.common.logging.Logger`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        name = kwargs.pop("name", None)
        trader_id = kwargs.pop("trader_id", None)

        if name is None:
            if args and isinstance(args[0], str):
                name = args[0]
            elif len(args) >= 2 and args[1] is not None:
                name = str(args[1])
            elif trader_id is not None:
                name = str(trader_id)
            else:
                name = type(self).__name__

        self._logger = _ComponentLogger(name=name)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._logger, item)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.error(message, *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.exception(message, *args, **kwargs)


class LoggerAdapter:
    def __init__(self, name: str, logger: Logger | _ComponentLogger) -> None:
        self._name = name
        self._logger = logger

    def _message(self, message: str) -> str:
        return f"[{self._name}] {message}"

    def debug(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.debug(self._message(message), *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.info(self._message(message), *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.warning(self._message(message), *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.error(self._message(message), *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return self._logger.exception(self._message(message), *args, **kwargs)
