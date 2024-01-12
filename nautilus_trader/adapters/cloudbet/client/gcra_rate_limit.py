from datetime import datetime, timedelta


class RateLimit:
    """
    Represents a rate limiting mechanism.

    Attributes:
        count (int): The maximum number of allowed accesses within the period.
        period (timedelta): The time period over which count is measured.

    The inverse property calculates the minimum time interval required between
    consecutive accesses, based on the count and period.
    """
    def __init__(self, count: int, period: timedelta) -> None:
        self.count = count
        self.period = period

    @property
    def inverse(self) -> float:
        return self.period.total_seconds() / self.count


class RateLimitStore:
    """
    An in-memory store to manage and enforce rate limits using Time-Against-Token (TAT) values.

    Methods:
        get_tat(key: str) -> datetime:
            Retrieves the last recorded TAT for the given key or the current time if the key is new.
        set_tat(key: str, tat: datetime) -> None:
            Sets the TAT for a specific key.
        update(key: str, limit: RateLimit) -> bool:
            Determines if a request identified by `key` is within the rate limit.
            If within limits, allows the request and updates the TAT for subsequent checks.
            Returns True if the request should be rejected, False otherwise.

    """
    def __init__(self):
        self.tats: [str, datetime] = {}  # In-memory store as a dictionary

    def get_tat(self, key: str) -> datetime:
        # Returns a previous TAT for the key or the current time if not present.
        return self.tats.get(key, datetime.utcnow())

    def set_tat(self, key: str, tat: datetime) -> None:
        # Sets the TAT for the given key.
        self.tats[key] = tat

    def update(self, key: str, limit: RateLimit) -> bool:
        now = datetime.utcnow()
        tat = max(self.get_tat(key), now)
        separation = (tat - now).total_seconds()
        max_interval = limit.period.total_seconds() - limit.inverse
        if separation > max_interval:
            reject = True
        else:
            reject = False
            new_tat = max(tat, now) + timedelta(seconds=limit.inverse)
            self.set_tat(key, new_tat)
        return reject
