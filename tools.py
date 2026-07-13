from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15


@tool
def get_weather_tool(city: str) -> dict[str, Any]:
    """
    Get the current weather for a city.

    Args:
        city (str): City name, e.g. "Colombo" or "San Francisco".

    Returns:
        dict[str, Any]: Current temperature (Celsius), windspeed, and the
        resolved location name, or an error if the city could not be found.
    """
    logger.info("get_weather_tool called: city=%s", city)
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=_HTTP_TIMEOUT,
        )
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return {"error": f"Could not find a location matching '{city}'."}
        place = results[0]
        latitude, longitude = place["latitude"], place["longitude"]

        forecast = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": latitude, "longitude": longitude, "current_weather": "true"},
            timeout=_HTTP_TIMEOUT,
        )
        forecast.raise_for_status()
        current = forecast.json().get("current_weather", {})
    except requests.RequestException:
        logger.exception("get_weather_tool failed calling Open-Meteo")
        return {"error": "Weather lookup failed."}

    return {
        "location": f"{place.get('name')}, {place.get('country', '')}".strip(", "),
        "temperature_celsius": current.get("temperature"),
        "windspeed_kmh": current.get("windspeed"),
        "observed_at": current.get("time"),
    }


@tool
def get_exchange_rate_tool(base_currency: str, target_currency: str, amount: float = 1.0) -> dict[str, Any]:
    """
    Convert an amount from one currency to another using current exchange rates.

    Args:
        base_currency (str): Three-letter currency code to convert from, e.g. "USD".
        target_currency (str): Three-letter currency code to convert to, e.g. "LKR".
        amount (float): Amount in the base currency to convert. Defaults to 1.0.

    Returns:
        dict[str, Any]: The converted amount and rate, or an error.
    """
    logger.info(
        "get_exchange_rate_tool called: base=%s target=%s amount=%s",
        base_currency,
        target_currency,
        amount,
    )
    try:
        response = requests.get(
            f"https://open.er-api.com/v6/latest/{base_currency.upper()}",
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        logger.exception("get_exchange_rate_tool failed calling exchange rate API")
        return {"error": "Exchange rate lookup failed."}

    if payload.get("result") != "success":
        return {"error": payload.get("error-type", "Exchange rate lookup failed.")}

    rate = payload.get("rates", {}).get(target_currency.upper())
    if rate is None:
        return {"error": f"No rate available for {base_currency} -> {target_currency}."}

    return {
        "base_currency": base_currency.upper(),
        "target_currency": target_currency.upper(),
        "amount": amount,
        "rate": rate,
        "converted_amount": round(rate * amount, 4),
        "date": payload.get("time_last_update_utc"),
    }


_COUNTRIES_DATASET_URL = "https://cdn.jsdelivr.net/gh/mledoze/countries@master/dist/countries.json"
_countries_cache: list[dict[str, Any]] | None = None


def _load_countries() -> list[dict[str, Any]]:
    global _countries_cache
    if _countries_cache is None:
        response = requests.get(_COUNTRIES_DATASET_URL, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        _countries_cache = response.json()
    return _countries_cache


@tool
def get_country_info_tool(country: str) -> dict[str, Any]:
    """
    Get basic facts about a country: capital, region, area, currencies, and languages.

    Args:
        country (str): Country name, e.g. "Sri Lanka".

    Returns:
        dict[str, Any]: Country facts, or an error if the country was not found.
    """
    logger.info("get_country_info_tool called: country=%s", country)
    try:
        countries = _load_countries()
    except requests.RequestException:
        logger.exception("get_country_info_tool failed loading the countries dataset")
        return {"error": "Country lookup failed."}

    needle = country.strip().lower()
    match = next(
        (
            c
            for c in countries
            if needle in c.get("name", {}).get("common", "").lower()
            or needle in c.get("name", {}).get("official", "").lower()
        ),
        None,
    )
    if not match:
        return {"error": f"No country found matching '{country}'."}

    return {
        "name": match.get("name", {}).get("common"),
        "capital": (match.get("capital") or [None])[0],
        "region": match.get("region"),
        "subregion": match.get("subregion"),
        "area_km2": match.get("area"),
        "currencies": list((match.get("currencies") or {}).keys()),
        "languages": list((match.get("languages") or {}).values()),
    }


NATIVE_TOOLS = [
    get_weather_tool,
    get_exchange_rate_tool,
    get_country_info_tool,
]
