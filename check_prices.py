"""
Flight price checker.

Reads trips.json and checks each trip's current price via SerpApi, then
sends a push notification (via ntfy.sh) to the trip owner's personal topic
if the price is at or below their target.

Two trip modes are supported (set via the "mode" field in trips.json):

  "specific" - exact departure_date / return_date.
               Uses SerpApi's google_flights engine.
               Example: "Guatemala City, Jan 9 to Jan 18, $400 or less"

  "flexible" - a whole month, any dates within it.
               Uses SerpApi's google_travel_explore engine.
               Example: "Guatemala City sometime in January, $400 or less"

Environment variables required:
  SERPAPI_KEY   - your SerpApi API key

Run manually:
  SERPAPI_KEY=xxxx python check_prices.py
"""

import json
import os
import sys
import urllib.request
import urllib.parse

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
TRIPS_FILE = "trips.json"
NTFY_BASE_URL = "https://ntfy.sh"
SERPAPI_BASE_URL = "https://serpapi.com/search.json"


def load_trips(path):
    with open(path, "r") as f:
        return json.load(f)


def call_serpapi(params):
    params = {**params, "api_key": SERPAPI_KEY}
    url = SERPAPI_BASE_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


def get_price_specific(trip):
    """
    Exact date pair. Uses the google_flights engine.
    Returns the cheapest round-trip price found, or None.
    """
    data = call_serpapi({
        "engine": "google_flights",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "outbound_date": trip["departure_date"],
        "return_date": trip["return_date"],
        "type": "1",  # round trip
        "currency": "USD",
    })

    if "error" in data:
        print(f"  SerpApi error: {data['error']}")
        return None

    candidates = []
    for key in ("best_flights", "other_flights"):
        for flight in data.get(key, []):
            price = flight.get("price")
            if isinstance(price, (int, float)):
                candidates.append(price)

    if not candidates:
        print("  No prices found.")
        return None

    return min(candidates)


def get_price_flexible(trip):
    """
    Whole-month search, any dates within it. Uses the google_travel_explore
    engine with a specific arrival_id (e.g. an airport code), which returns
    flight price info for that exact destination within the given month.
    Returns the cheapest price found, or None.
    """
    data = call_serpapi({
        "engine": "google_travel_explore",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "month": trip["month"],       # 1-12
        "currency": "USD",
    })

    if "error" in data:
        print(f"  SerpApi error: {data['error']}")
        return None

    candidates = []
    for dest in data.get("destinations", []):
        price = dest.get("flight_price")
        if isinstance(price, (int, float)):
            candidates.append(price)
        # Some responses nest price info inside a "flight" object instead.
        flight = dest.get("flight")
        if isinstance(flight, dict):
            price = flight.get("price")
            if isinstance(price, (int, float)):
                candidates.append(price)

    if not candidates:
        print("  No prices found.")
        return None

    return min(candidates)


def get_price(trip):
    mode = trip.get("mode", "specific")
    if mode == "specific":
        return get_price_specific(trip)
    elif mode == "flexible":
        return get_price_flexible(trip)
    else:
        print(f"  Unknown mode '{mode}', skipping.")
        return None


def send_ntfy_alert(topic, title, message):
    url = f"{NTFY_BASE_URL}/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "airplane,moneybag",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def describe_dates(trip):
    if trip.get("mode") == "flexible":
        month_names = ["", "January", "February", "March", "April", "May", "June",
                        "July", "August", "September", "October", "November", "December"]
        return f"sometime in {month_names[trip['month']]}"
    return f"depart {trip['departure_date']}, return {trip['return_date']}"


def main():
    if not SERPAPI_KEY:
        print("ERROR: SERPAPI_KEY environment variable is not set.")
        sys.exit(1)

    trips = load_trips(TRIPS_FILE)
    print(f"Checking {len(trips)} trip(s)...")

    for trip in trips:
        print(f"\n{trip['label']} ({trip['origin']} -> {trip['destination']})")
        price = get_price(trip)

        if price is None:
            continue

        print(f"  Current cheapest price: ${price}")
        print(f"  Target: ${trip['target_price_usd']}")

        if price <= trip["target_price_usd"]:
            print("  -> Price hit! Sending notification.")
            send_ntfy_alert(
                topic=trip["ntfy_topic"],
                title=f"Price drop: {trip['origin']} -> {trip['destination']}",
                message=(
                    f"{trip['label']}\n"
                    f"${price} round-trip (target was ${trip['target_price_usd']})\n"
                    f"{describe_dates(trip)}"
                ),
            )
        else:
            print("  -> No alert (above target).")

    print("\nDone.")


if __name__ == "__main__":
    main()