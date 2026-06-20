"""
Flight price checker.

Reads trips.json and checks each trip's current price via SerpApi, then
sends a push notification (via ntfy.sh) to the trip owner's personal topic
if the price is at or below their target.

Two trip modes are supported (set via the "mode" field in trips.json):

  "specific" - exact departure_date / return_date.
               Uses SerpApi's google_flights engine.
               Example: "Guatemala City, Jan 9 to Jan 18, $400 or less"

  "flexible" - whatever dates are cheapest right now, no month targeting.
               Uses SerpApi's google_travel_explore engine.
               Example: "Guatemala City, whenever it's cheap, $400 or less"

Optional field for either mode:

  "nonstop_only": true   - only consider nonstop (direct) flights.
                            Omit or set to false to allow any number of stops.

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
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  HTTP {e.code} from SerpApi. Response body: {body}")
        return {"error": f"HTTP {e.code}: {body}"}


def get_price_specific(trip):
    """
    Exact date pair. Uses the google_flights engine.
    Returns (price, start_date, end_date), or (None, None, None).
    """
    params = {
        "engine": "google_flights",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "outbound_date": trip["departure_date"],
        "return_date": trip["return_date"],
        "type": "1",  # round trip
        "currency": "USD",
    }
    if trip.get("nonstop_only"):
        params["stops"] = "1"  # 1 = nonstop only

    data = call_serpapi(params)

    if "error" in data:
        print(f"  SerpApi error: {data['error']}")
        return None, None, None

    candidates = []
    for key in ("best_flights", "other_flights"):
        for flight in data.get(key, []):
            price = flight.get("price")
            if isinstance(price, (int, float)):
                candidates.append(price)

    if not candidates:
        print("  No prices found.")
        return None, None, None

    # Specific mode already has known dates from trips.json, but we
    # return them here too so get_price() has one consistent shape.
    return min(candidates), trip["departure_date"], trip["return_date"]


def get_price_flexible(trip):
    """
    Flexible dates to a specific destination. Uses the google_travel_explore
    engine with both departure_id and arrival_id set.

    Important: when arrival_id is a specific airport, SerpApi returns a
    "flights" array (same shape as the google_flights engine) along with
    top-level "start_date" / "end_date" fields for whatever window it found
    to be cheapest right now - it does NOT support a "month" parameter
    combined with a specific arrival_id (that combo returns an error,
    since month-based scanning only applies to the open-ended
    "destinations" discovery mode with no arrival_id set).

    The length of that window isn't fixed - it could be a few days, a
    week, ten days, etc. - so we read the actual start_date/end_date
    SerpApi gives us instead of assuming any particular trip length.

    Returns (price, start_date, end_date), or (None, None, None).
    """
    params = {
        "engine": "google_travel_explore",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "currency": "USD",
    }
    if trip.get("nonstop_only"):
        params["stops"] = "1"  # 1 = nonstop only

    data = call_serpapi(params)

    if "error" in data:
        print(f"  SerpApi error: {data['error']}")
        return None, None, None

    candidates = []
    for flight in data.get("flights", []):
        price = flight.get("price")
        if isinstance(price, (int, float)):
            candidates.append(price)

    if not candidates:
        print("  No prices found.")
        return None, None, None

    start_date = data.get("start_date")
    end_date = data.get("end_date")
    return min(candidates), start_date, end_date


def get_price(trip):
    """
    Returns (price, start_date, end_date). Any of these may be None
    if nothing could be found or the mode is unrecognized.
    """
    mode = trip.get("mode", "specific")
    if mode == "specific":
        return get_price_specific(trip)
    elif mode == "flexible":
        return get_price_flexible(trip)
    else:
        print(f"  Unknown mode '{mode}', skipping.")
        return None, None, None


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


def describe_dates(start_date, end_date):
    if start_date and end_date:
        return f"depart {start_date}, return {end_date}"
    if start_date:
        return f"around {start_date}"
    return "dates unknown"


def main():
    if not SERPAPI_KEY:
        print("ERROR: SERPAPI_KEY environment variable is not set.")
        sys.exit(1)

    trips = load_trips(TRIPS_FILE)
    print(f"Checking {len(trips)} trip(s)...")

    for trip in trips:
        print(f"\n{trip['label']} ({trip['origin']} -> {trip['destination']})")
        price, start_date, end_date = get_price(trip)

        if price is None:
            continue

        print(f"  Current cheapest price: ${price}")
        print(f"  Target: ${trip['target_price_usd']}")
        print(f"  Dates found: {describe_dates(start_date, end_date)}")

        if price <= trip["target_price_usd"]:
            print("  -> Price hit! Sending notification.")
            send_ntfy_alert(
                topic=trip["ntfy_topic"],
                title=f"Price drop: {trip['origin']} -> {trip['destination']}",
                message=(
                    f"{trip['label']}\n"
                    f"${price} round-trip (target was ${trip['target_price_usd']})\n"
                    f"{describe_dates(start_date, end_date)}"
                ),
            )
        else:
            print("  -> No alert (above target).")

    print("\nDone.")


if __name__ == "__main__":
    main()
