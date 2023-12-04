import requests
import json
import time
from typing import Optional
from enum import Enum


class Cloudbet:
    api_key = "eyJhbGciOiJSUzI1NiIsImtpZCI6IkhKcDkyNnF3ZXBjNnF3LU9rMk4zV05pXzBrRFd6cEdwTzAxNlRJUjdRWDAiLCJ0eXAiOiJKV1QifQ.eyJhY2Nlc3NfdGllciI6InRyYWRpbmciLCJleHAiOjIwMDAyODY2NzIsImlhdCI6MTY4NDkyNjY3MiwianRpIjoiOWExMTE3N2MtNzFiNi00ZDZlLTg4MDgtOTBkOTU0YjU2ODNhIiwic3ViIjoiMTI1MzBjYjMtMjNiYS00NDE4LWIwZTAtZTgyZmZjYjIwOTQ1IiwidGVuYW50IjoiY2xvdWRiZXQiLCJ1dWlkIjoiMTI1MzBjYjMtMjNiYS00NDE4LWIwZTAtZTgyZmZjYjIwOTQ1In0.B5zMxNj-gTv2DEwAB4bgJHUgfwdYvHhcaAZOBAgOankI6JTl-jbdbfG98AsCLAQ199Ph_Ztyc8xWARYrkttYZTNpTfX2eu5YiWckRK-TOAeRE6GSNyUYnwQNHnZNtgaPqUaUpH6Z9-0SQ6R30n20u7WGw7KV2Wuwyiv2fbSsIQx8eBOrIIjguROPDzfShZUAiplvIPN6vOLHWCyHZzF_7fMVOkPazfNNJ7nPwDKQKm3-1_ruvcSrD_WkujyExv94444swCKhbWqJuVewXGmD02GQdTIRS876copduCzMYcHEKUjiCodoAGIDDkJ1mMMdokafchoUB9sNA_jVXZ2prw"
    api_url = 'https://sports-api.cloudbet.com'

    def __init__(self):
        self.api = requests.Session()
        # self.api.headers.update({'X-API-Key': self.api_key, 'Accept': 'application/json', 'Content-Type': 'application/json'})

    def get_balance(self):
        return self.api.get_balance()

    def get_sports(self):
        try:
            sports_response = requests.get(self.api_url + "/pub/v2/odds/sports",
                                           headers={'X-API-Key': self.api_key, 'Accept': 'application/json',
                                                    'Content-Type': 'application/json'})
            if sports_response.status_code == 200:
                # save response to json file
                with open('data/sports_response.json', 'w') as outfile:
                    json.dump(sports_response.json(), outfile)
                print(sports_response.json())
                return sports_response.json()
        except Exception as e:
            print(e)

    def get_all_sport_keys(self):
        # get all sports
        sports = self.get_sports()
        # get all sport keys
        sport_keys = []
        for sport in sports['sports']:
            sport_keys.append(sport['key'])
        # filter out duplicate sport keys/get unique sport keys
        sport_keys = list(dict.fromkeys(sport_keys))
        return sport_keys

    def get_competition_for_sport(self, sport_key):
        try:
            competition_response = requests.get(self.api_url + "/pub/v2/odds/sports/" + sport_key,
                                                headers={'X-API-Key': self.api_key, 'Accept': 'application/json',
                                                         'Content-Type': 'application/json'})
            if competition_response.status_code == 200:
                # save response to json file
                # with open('data/competition_response.json', 'w') as outfile:
                #     json.dump(competition_response.json(), outfile)
                # print(competition_response.json())
                return competition_response.json()
        except Exception as e:
            print(e)

    def get_events_for_competition(self, competitionkey):
        try:
            events_response = requests.get(self.api_url + "/pub/v2/odds/competitions/" + competitionkey,
                                           headers={'X-API-Key': self.api_key, 'Accept': 'application/json',
                                                    'Content-Type': 'application/json'})
            if events_response.status_code == 200:
                # save response to json file
                # with open('data/events_response.json', 'w') as outfile:
                #     json.dump(events_response.json(), outfile)
                # print(events_response.json())
                return events_response.json()
            else:
                print(events_response.json())
                return None
        except Exception as e:
            print(e)

    def get_events_for_sport(self, sport_key):
        # Cloudbet API supports query parameters for filtering events by status, start date, end date, and markets.
        # NB: for now we only want to query events that are open for pre-match betting, so we will use the status query parameter PRE_TRADING or TRADING.
        # when status is false, the cloud bet api returns all events for the sport with status PRE_TRADING or TRADING
        # in the future we can use the start date and end date query parameters to query events for a specific date range
        fixture_status = "false"
        try:
            # we're interested in pre-match events only, so we will set live to false and filter events starting in the next 4 hours at a time
            # get current unix timestamp as a string
            current_timestamp = str(int(time.time()))
            # get timestamp for 4 hours from now as a string
            future_timestamp = str(int(time.time()) + 24*60*60)
            # add query parameters to url
            query_params = {
                'sport': sport_key,
                'live': 'false',
                'from': current_timestamp,
                'to': future_timestamp,
                'limit': '10',
            }
            event_response = requests.get(self.api_url + "/pub/v2/odds/events", params=query_params,
                                          headers={'X-API-Key': self.api_key, 'Accept': 'application/json',
                                                   'Content-Type': 'application/json'})
            if event_response.status_code == 200:
                # save response to json file
                with open('data/test_get_events_for_sport_response.json', 'w') as outfile:
                    json.dump(event_response.json(), outfile)
                    print(event_response.json())
                return event_response.json()
            else:
                print(event_response.json())
                return None
        except Exception as e:
            print(e)


class Side(Enum):
    AWAY = "AWAY"
    HOME = "HOME"


class selection_type(Enum):
    BACK = "BACK"
    LAY = "LAY"


class Selection(Enum):
    # NB: the market params must be built dynamically based on the market type
    market_params: Optional[str] = None
    max_stake: Optional[str] = None
    min_stake: Optional[str] = None
    status: Optional[str] = None
    price: Optional[str] = None
    type: Optional[selection_type] = None


class Fixture:
    sport_name: Optional[str] = None
    competition_name: Optional[str] = None
    # this is dynamically built based on the event data
    event_name: Optional[str] = None
    market_name: Optional[str] = None
    market_slug: Optional[str] = None
    side: Optional[Side] = None
    selection: Optional[Selection] = 0


def extract_fixture_by_event(event):
    return Fixture(
        sport_name=event['sport_name'],
        competition_name=event['competition_name'],
        event_name=event['event_name'],
        market_name=event['market_name'],
        market_slug=event['market_slug'],
        side=event['side'],
        selection=Selection(
            market_params=event['market_params'],
            max_stake=event['max_stake'],
            min_stake=event['min_stake'],
            status=event['status'],
            price=event['price'],
            type=event['type'],
        )
    )


def generate_fixture():
    cb = Cloudbet()
    sport_keys = cb.get_all_sport_keys()
    # get events for each sport
    for sport_key in sport_keys:
        competitions_list = cb.get_events_for_sport(sport_key)['competitions']
        if competitions_list is None:
            print("No competitions for sport: " + sport_key)
            continue
        # iterate through each competition
        for competition in competitions_list:
            competition_name = competition['key']
            sport_name = competition['sport']['key']
            # itereate through each event
            for event in competition['events']:
                Fixture(sport_name=sport_name,
                        competition_name=competition_name,
                        )


# driver code
if __name__ == '__main__':
    cb = Cloudbet()
    cb.get_sports()
    # for sport in cb.get_sports()['sports']:
    #     cb.get_events_for_sport(str(sport['key']))
    #
    # # # cb.get_competition_for_sport("soccer")
    # # generate_fixture()
