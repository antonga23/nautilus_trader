import time
from datetime import timedelta,datetime

import pytest

from nautilus_trader.adapters.cloudbet.client.gcra_rate_limit import RateLimitStore, RateLimit
import time
from datetime import timedelta, datetime

import pytest

from nautilus_trader.adapters.cloudbet.client.gcra_rate_limit import RateLimitStore, RateLimit

class TestRateLimiter:
    # def setup(self):
    #     # Fixture Setup
    #     # self.loop = asyncio.get_event_loop()
    #     # self.clock = LiveClock()
    #     # self.logger = Logger(clock=self.clock, bypass=True)
    #     # self.client = CloudbetClient(self.loop, self.logger)
    #     # # we explicitly need to set the api key and secret to test credentials
    #     # self.client._api_key = test_api_key
    #     # self.client._api_url = test_api_url
    #
    #     self.rate_limit = RateLimit(count=4, period=timedelta(seconds=60)) # allow 4 requests per 60 seconds
    #     self.store = RateLimitStore()

    @pytest.fixture
    def rate_limit(self):
        return RateLimit(count=5, period=timedelta(seconds=5))

    @pytest.fixture
    def store(self):
        return RateLimitStore()

    def test_inverse_calculation(self):
        count = 5  # You can vary this for different tests
        period = timedelta(seconds=50)  # Vary this as well
        rate_limit = RateLimit(count=count, period=period)

        expected_inverse = period.total_seconds() / count
        assert rate_limit.inverse == expected_inverse

    def test_initial_tat_retrieval(self, store):
        key = "new_key"
        print(store.get_tat(key))
        assert abs(store.get_tat(key) - datetime.utcnow()) < timedelta(seconds=1)

    def test_tat_setting_and_retrieval(self,store):
        key = "test_key"
        tat_time = datetime.utcnow() + timedelta(seconds=50)
        store.set_tat(key, tat_time)
        assert store.get_tat(key) == tat_time
    #
    @pytest.mark.asyncio
    async def test_update_no_prior_access(self, store, rate_limit):
        key = "new_key"
        assert store.update(key, rate_limit) == False
    #
    @pytest.mark.asyncio
    async def test_update_under_rate_limit(self, store, rate_limit):
        key = "under_limit_key"
        for _ in range(rate_limit.count):
            assert not store.update(key, rate_limit)
    #
    @pytest.mark.asyncio
    async def test_update_over_rate_limit(self, store, rate_limit):
        key = "over_limit_key"
        for _ in range(rate_limit.count):
            store.update(key, rate_limit)
        # limit reached, subsequent calls should be rejected
        assert store.update(key, rate_limit) == True

    @pytest.mark.asyncio
    async def test_update_after_period_expires(self, store, rate_limit) :
        key = "after_period_key"
        assert (store.update(key, rate_limit)) == False
        time.sleep(rate_limit.period.total_seconds())
        assert (store.update(key, rate_limit)) == False

    @pytest.mark.asyncio
    def test_update_at_boundary_condition(self, store, rate_limit):
        key = "boundary_key"
        assert store.update(key, rate_limit) == False
        time.sleep(rate_limit.inverse) # simulate advancing time to the boundary
        assert store.update(key, rate_limit) == False
