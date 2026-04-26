import copy
import logging
import json
import os
import re
import time
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_user, logout_user, login_required, current_user
import requests
from sqlalchemy import func, or_, extract, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload
from .ai_usage import add_ai_usage_log, check_ai_quota, estimate_request_units
from .audit import add_audit_log
from .models import (
    db,
    User,
    Food,
    MealPlan,
    MealPlanItem,
    StudentDetail,
    Attendance,
    HealthMetric,
    ApprovalRequest,
    utcnow,
)
from .notifications import broadcast_school_notification
from .security import establish_session
from .extensions import bcrypt, limiter # Imported from File 1 for password hashing

# --- AI Model Initialization (from File 1) ---
from dotenv import load_dotenv
import google.generativeai as genai

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_gemini_model = None
_gemini_model_signature = (None, None)
logger = logging.getLogger(__name__)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _load_ai_api_key():
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=False)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
    return api_key

def _gemini_model_name():
    return os.environ.get("GEMINI_MODEL_NAME", "gemini-2.5-flash")

def _gemini_timeout_seconds():
    return _env_float("GEMINI_REQUEST_TIMEOUT_SECONDS", 8.0)

def _gemini_api_base_url():
    return os.environ.get("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

def _nutrition_generation_config():
    return genai.types.GenerationConfig(
        temperature=0,
        max_output_tokens=_env_int("GEMINI_NUTRITION_MAX_OUTPUT_TOKENS", 120),
        response_mime_type="application/json",
    )

def _recipe_generation_config():
    return genai.types.GenerationConfig(
        temperature=0.2,
        max_output_tokens=_env_int("GEMINI_RECIPE_MAX_OUTPUT_TOKENS", 512),
        response_mime_type="application/json",
    )

def _meal_plan_generation_config():
    return genai.types.GenerationConfig(
        temperature=0.2,
        max_output_tokens=_env_int("GEMINI_MEAL_PLAN_MAX_OUTPUT_TOKENS", 768),
        response_mime_type="application/json",
    )

def _health_generation_config():
    return genai.types.GenerationConfig(
        temperature=0.1,
        max_output_tokens=_env_int("GEMINI_HEALTH_MAX_OUTPUT_TOKENS", 768),
        response_mime_type="application/json",
    )

def _friendly_ai_error(error):
    message = str(error)
    lower_message = message.lower()
    if "api key not valid" in lower_message or "permission denied" in lower_message or "unauthenticated" in lower_message:
        return "The Gemini API key is invalid. Update GOOGLE_API_KEY in your environment variables and restart the service."
    if "reported as leaked" in lower_message:
        return "This Gemini API key was reported as leaked. Generate a new key in Google AI Studio and update GOOGLE_API_KEY."
    if "503" in lower_message or "service unavailable" in lower_message:
        return "The Gemini API is temporarily unavailable. Please retry in a moment."
    if "deadline exceeded" in lower_message or "timed out" in lower_message or "timeout" in lower_message:
        return "The AI service took too long to respond. Please try again with a simpler search."
    if "api_key_invalid" in lower_message or "api key expired" in lower_message or "expired" in lower_message:
        return "The Gemini API key is invalid or expired. Update GOOGLE_API_KEY in your environment variables, then try again."
    if "not found" in lower_message and "models/" in lower_message:
        return "The configured Gemini model is not available. Set GEMINI_MODEL_NAME=gemini-2.5-flash and redeploy."
    if "quota" in lower_message or "resource_exhausted" in lower_message:
        return "The Gemini API quota is exhausted for this key. Please check quota/billing in Google AI Studio."
    return "The AI service could not complete this request. Please try again."

def _clear_ai_response_caches():
    for cache_name in ("_cached_ai_nutrition_lookup", "_cached_ai_recipe_lookup"):
        cached_fn = globals().get(cache_name)
        if cached_fn is not None and hasattr(cached_fn, "cache_clear"):
            cached_fn.cache_clear()

def _reset_gemini_model_cache():
    global _gemini_model, _gemini_model_signature
    _gemini_model = None
    _gemini_model_signature = (None, None)
    _clear_ai_response_caches()

def _build_gemini_model():
    global _gemini_model, _gemini_model_signature
    api_key = _load_ai_api_key()
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not set.")
    model_name = _gemini_model_name()
    signature = (api_key, model_name)

    if _gemini_model is None or _gemini_model_signature != signature:
        genai.configure(api_key=api_key)
        _gemini_model = genai.GenerativeModel(model_name)
        _gemini_model_signature = signature
        _clear_ai_response_caches()

    return _gemini_model

def _generation_config_to_rest(generation_config):
    if generation_config is None:
        return {}

    field_map = {
        "temperature": "temperature",
        "top_p": "topP",
        "top_k": "topK",
        "candidate_count": "candidateCount",
        "max_output_tokens": "maxOutputTokens",
        "response_mime_type": "responseMimeType",
    }

    rest_config = {}
    for source_field, target_field in field_map.items():
        value = getattr(generation_config, source_field, None)
        if value is not None:
            rest_config[target_field] = value

    return rest_config

def _gemini_rest_url():
    return f"{_gemini_api_base_url()}/models/{_gemini_model_name()}:generateContent"

def _extract_rest_text(payload):
    prompt_feedback = payload.get("promptFeedback") or {}
    if prompt_feedback.get("blockReason"):
        raise ValueError(f"AI response was blocked due to safety concerns: {prompt_feedback['blockReason']}.")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise ValueError("AI returned no candidates.")

    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text_chunks = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")]
    if not text_chunks:
        raise ValueError("AI returned no text parts.")

    return "".join(text_chunks)

def _call_gemini_rest_json(prompt, *, generation_config):
    api_key = _load_ai_api_key()
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not set.")

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ]
    }

    rest_generation_config = _generation_config_to_rest(generation_config)
    if rest_generation_config:
        body["generationConfig"] = rest_generation_config

    response = requests.post(
        _gemini_rest_url(),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=_gemini_timeout_seconds(),
    )

    if not response.ok:
        try:
            payload = response.json()
            api_message = ((payload.get("error") or {}).get("message")) or response.text
        except ValueError:
            api_message = response.text
        raise ValueError(f"Gemini API error {response.status_code}: {api_message}")

    payload = response.json()
    return _parse_ai_json_response(_extract_rest_text(payload))

def _normalize_food_query(value):
    return " ".join((value or "").split())

def _food_to_nutrition_payload(food):
    return {
        "calories": food.calories,
        "protein": food.protein,
        "carbs": food.carbs,
        "fats": food.fats,
    }

def _food_to_search_payload(food):
    payload = _food_to_nutrition_payload(food)
    payload["name"] = food.name
    return payload

FALLBACK_FOOD_NUTRITION = {
    "apple": {"calories": 52, "protein": 0.3, "carbs": 14.0, "fats": 0.2},
    "banana": {"calories": 89, "protein": 1.1, "carbs": 23.0, "fats": 0.3},
    "dal": {"calories": 116, "protein": 9.0, "carbs": 20.0, "fats": 0.4},
    "dosa": {"calories": 184, "protein": 4.5, "carbs": 28.3, "fats": 5.4},
    "idli": {"calories": 146, "protein": 4.5, "carbs": 29.0, "fats": 0.4},
    "jalebi": {"calories": 459, "protein": 4.6, "carbs": 65.0, "fats": 20.8},
    "paneer": {"calories": 265, "protein": 18.3, "carbs": 1.2, "fats": 20.8},
    "poha": {"calories": 130, "protein": 2.6, "carbs": 23.0, "fats": 2.4},
    "rice": {"calories": 130, "protein": 2.4, "carbs": 28.0, "fats": 0.3},
    "roti": {"calories": 297, "protein": 9.6, "carbs": 57.9, "fats": 3.7},
    "samosa": {"calories": 308, "protein": 5.0, "carbs": 32.0, "fats": 18.0},
    "sushi": {"calories": 143, "protein": 6.0, "carbs": 24.0, "fats": 2.2},
    "upma": {"calories": 156, "protein": 3.6, "carbs": 26.7, "fats": 4.8},
}

FALLBACK_RECIPE_LIBRARY = {
    "sushi": {
        "recipe_title": "Simple Veg Sushi Rolls",
        "ingredients": [
            "1 cup cooked sushi rice or short-grain rice",
            "2 nori sheets",
            "1/2 cucumber cut into thin strips",
            "1 small carrot cut into thin strips",
            "1/2 avocado sliced",
            "1 teaspoon rice vinegar",
            "Soy sauce for serving",
        ],
        "instructions": [
            "Mix the cooked rice with rice vinegar and let it cool slightly.",
            "Place a nori sheet on a flat surface and spread a thin layer of rice over it.",
            "Arrange cucumber, carrot, and avocado in a line near one edge.",
            "Roll tightly, slice into bite-sized pieces, and serve with soy sauce.",
        ],
    },
}

FALLBACK_MEAL_LIBRARY = {
    "Vegetarian": {
        "Breakfast": [
            {"meal_name": "Vegetable Poha", "calories": 320, "protein": 8, "carbs": 52, "fats": 8},
            {"meal_name": "Moong Dal Chilla", "calories": 290, "protein": 14, "carbs": 28, "fats": 10},
            {"meal_name": "Vegetable Upma", "calories": 300, "protein": 7, "carbs": 47, "fats": 9},
        ],
        "Lunch": [
            {"meal_name": "Rajma Rice Bowl", "calories": 520, "protein": 16, "carbs": 82, "fats": 12},
            {"meal_name": "Paneer Roti Plate", "calories": 540, "protein": 24, "carbs": 48, "fats": 24},
            {"meal_name": "Vegetable Khichdi", "calories": 450, "protein": 14, "carbs": 67, "fats": 12},
        ],
        "Dinner": [
            {"meal_name": "Mixed Dal with Phulka", "calories": 480, "protein": 19, "carbs": 58, "fats": 14},
            {"meal_name": "Palak Paneer with Roti", "calories": 510, "protein": 22, "carbs": 40, "fats": 28},
            {"meal_name": "Millet Khichdi", "calories": 430, "protein": 13, "carbs": 60, "fats": 11},
        ],
        "Snack": [
            {"meal_name": "Fruit and Yogurt Bowl", "calories": 190, "protein": 8, "carbs": 27, "fats": 5},
            {"meal_name": "Roasted Chana", "calories": 170, "protein": 9, "carbs": 22, "fats": 4},
        ],
        "Evening Snack": [
            {"meal_name": "Sprout Chaat", "calories": 210, "protein": 11, "carbs": 26, "fats": 6},
            {"meal_name": "Banana Peanut Smoothie", "calories": 240, "protein": 9, "carbs": 32, "fats": 8},
        ],
    },
    "Non-Vegetarian": {
        "Breakfast": [
            {"meal_name": "Egg Bhurji Toast", "calories": 320, "protein": 17, "carbs": 24, "fats": 16},
            {"meal_name": "Chicken Sandwich", "calories": 340, "protein": 22, "carbs": 30, "fats": 11},
        ],
        "Lunch": [
            {"meal_name": "Chicken Rice Bowl", "calories": 560, "protein": 31, "carbs": 58, "fats": 20},
            {"meal_name": "Egg Curry with Roti", "calories": 520, "protein": 24, "carbs": 41, "fats": 22},
        ],
        "Dinner": [
            {"meal_name": "Grilled Chicken with Veggies", "calories": 490, "protein": 34, "carbs": 24, "fats": 24},
            {"meal_name": "Fish Curry with Rice", "calories": 540, "protein": 28, "carbs": 49, "fats": 24},
        ],
        "Snack": [
            {"meal_name": "Boiled Eggs and Fruit", "calories": 210, "protein": 13, "carbs": 16, "fats": 9},
            {"meal_name": "Curd Chicken Wrap", "calories": 250, "protein": 17, "carbs": 20, "fats": 10},
        ],
        "Evening Snack": [
            {"meal_name": "Tuna Corn Salad", "calories": 220, "protein": 18, "carbs": 12, "fats": 10},
            {"meal_name": "Egg and Veg Roll", "calories": 260, "protein": 15, "carbs": 22, "fats": 11},
        ],
    },
    "Vegan": {
        "Breakfast": [
            {"meal_name": "Peanut Poha", "calories": 330, "protein": 9, "carbs": 50, "fats": 10},
            {"meal_name": "Tofu Bhurji Wrap", "calories": 310, "protein": 14, "carbs": 29, "fats": 13},
        ],
        "Lunch": [
            {"meal_name": "Chana Rice Bowl", "calories": 500, "protein": 17, "carbs": 77, "fats": 11},
            {"meal_name": "Tofu Stir Fry with Rice", "calories": 480, "protein": 19, "carbs": 53, "fats": 16},
        ],
        "Dinner": [
            {"meal_name": "Soya Curry with Roti", "calories": 490, "protein": 24, "carbs": 46, "fats": 18},
            {"meal_name": "Dal Millet Bowl", "calories": 440, "protein": 16, "carbs": 62, "fats": 11},
        ],
        "Snack": [
            {"meal_name": "Roasted Makhana Mix", "calories": 180, "protein": 6, "carbs": 22, "fats": 7},
            {"meal_name": "Fruit Chaat", "calories": 160, "protein": 3, "carbs": 34, "fats": 1},
        ],
        "Evening Snack": [
            {"meal_name": "Hummus Veg Sandwich", "calories": 230, "protein": 8, "carbs": 29, "fats": 8},
            {"meal_name": "Banana Oat Smoothie", "calories": 250, "protein": 7, "carbs": 39, "fats": 6},
        ],
    },
}

def _find_exact_food(food_name, school_scope_id=None):
    normalized_name = _normalize_food_query(food_name)
    if not normalized_name:
        return None

    return _school_food_query(school_scope_id).filter(func.lower(Food.name) == normalized_name.lower()).first()

def _search_local_foods(query, limit=6, school_scope_id=None):
    normalized_query = _normalize_food_query(query)
    if not normalized_query:
        return []

    food_query = _school_food_query(school_scope_id)
    exact_matches = food_query.filter(func.lower(Food.name) == normalized_query.lower()).order_by(Food.name).all()
    partial_matches = (
        food_query.filter(Food.name.ilike(f"%{normalized_query}%"))
        .order_by(Food.name)
        .limit(limit)
        .all()
    )

    ordered_matches = []
    seen_ids = set()
    for food in exact_matches + partial_matches:
        if food.id in seen_ids:
            continue
        ordered_matches.append(food)
        seen_ids.add(food.id)
        if len(ordered_matches) >= limit:
            break

    return ordered_matches

def _normalize_lookup_key(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

def _fallback_nutrition_lookup(food_name):
    normalized = _normalize_lookup_key(food_name)
    if not normalized:
        return None

    for candidate, nutrition in FALLBACK_FOOD_NUTRITION.items():
        if candidate == normalized or candidate in normalized or normalized in candidate:
            return copy.deepcopy(nutrition)

    if any(keyword in normalized for keyword in ("sweet", "jalebi", "halwa", "laddu")):
        return {"calories": 380, "protein": 4, "carbs": 58, "fats": 14}
    if any(keyword in normalized for keyword in ("fruit", "apple", "banana", "papaya", "mango")):
        return {"calories": 72, "protein": 0.8, "carbs": 18, "fats": 0.4}
    if any(keyword in normalized for keyword in ("rice", "bowl", "khichdi", "poha", "upma")):
        return {"calories": 165, "protein": 4.5, "carbs": 29, "fats": 3.5}
    if any(keyword in normalized for keyword in ("paneer", "tofu", "egg", "chicken", "fish")):
        return {"calories": 220, "protein": 19, "carbs": 6, "fats": 13}

    return {"calories": 180, "protein": 6, "carbs": 24, "fats": 6}

def _parse_ai_json_response(text):
    cleaned = (text or "").strip().replace("```json", "").replace("```", "").strip()
    if not cleaned:
        raise ValueError("AI returned an empty response.")

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
        if not match:
            raise ValueError("AI returned an invalid JSON format.")
        return json.loads(match.group(0))

def _coerce_nutrition_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("AI returned an invalid nutrition response.")
    if payload.get("error"):
        raise ValueError(str(payload["error"]))

    try:
        return {
            "calories": float(payload["calories"]),
            "protein": float(payload["protein"]),
            "carbs": float(payload["carbs"]),
            "fats": float(payload["fats"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("AI returned incomplete nutrition data.") from exc

def _coerce_recipe_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("AI returned an invalid recipe response.")

    ingredients = [str(item).strip() for item in payload.get("ingredients", []) if str(item).strip()]
    instructions = [str(item).strip() for item in payload.get("instructions", []) if str(item).strip()]

    if not ingredients or not instructions:
        raise ValueError("AI returned an incomplete recipe.")

    return {
        "recipe_title": str(payload.get("recipe_title") or "Recipe").strip(),
        "ingredients": ingredients,
        "instructions": instructions,
        "nutrition": _coerce_nutrition_payload(payload.get("nutrition") or {}),
    }

def _coerce_meal_plan_payload(payload, expected_meals=None):
    if not isinstance(payload, list) or not payload:
        raise ValueError("AI returned an invalid meal plan.")

    normalized_meals = []
    for index, meal in enumerate(payload):
        if not isinstance(meal, dict):
            continue
        meal_type = str(meal.get("meal_type") or f"Meal {index + 1}").strip()
        meal_name = str(meal.get("meal_name") or f"{meal_type} option").strip()
        nutrition = _coerce_nutrition_payload(meal)
        normalized_meals.append({
            "meal_type": meal_type,
            "meal_name": meal_name,
            **nutrition,
        })

    if not normalized_meals:
        raise ValueError("AI returned an empty meal plan.")

    if expected_meals:
        return normalized_meals[:expected_meals]
    return normalized_meals

def _coerce_health_insights_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("AI returned an invalid health report.")

    bmi_analysis = payload.get("bmi_analysis") or {}
    positive_points = [str(point).strip() for point in payload.get("positive_points", []) if str(point).strip()]
    areas = []
    for item in payload.get("areas_for_improvement", []):
        if not isinstance(item, dict):
            continue
        point = str(item.get("point", "")).strip()
        recommendation = str(item.get("recommendation", "")).strip()
        if point and recommendation:
            areas.append({"point": point, "recommendation": recommendation})

    if not bmi_analysis.get("category") or not bmi_analysis.get("comment"):
        raise ValueError("AI returned an incomplete BMI analysis.")
    if not positive_points:
        raise ValueError("AI returned no positive points.")
    if not areas:
        raise ValueError("AI returned no improvement areas.")

    return {
        "overall_summary": str(payload.get("overall_summary") or "Here is a helpful health summary.").strip(),
        "bmi_analysis": {
            "category": str(bmi_analysis["category"]).strip(),
            "comment": str(bmi_analysis["comment"]).strip(),
        },
        "positive_points": positive_points[:3],
        "areas_for_improvement": areas[:3],
    }

def _parse_preference_list(value):
    return {
        token.strip().lower()
        for token in re.split(r"[,/]", value or "")
        if token.strip()
    }

def _meal_count_number(value):
    match = re.search(r"\d+", value or "")
    if not match:
        return 3
    count = int(match.group(0))
    return min(max(count, 3), 5)

def _meal_slots_for_count(meal_count):
    slots = ["Breakfast", "Lunch", "Dinner", "Snack", "Evening Snack"]
    return slots[:_meal_count_number(meal_count)]

def _select_fallback_meal_option(options, avoid_terms, index):
    filtered = [
        option for option in options
        if not any(term in option["meal_name"].lower() for term in avoid_terms)
    ]
    source = filtered or options
    selected = copy.deepcopy(source[index % len(source)])
    return selected

def _fallback_meal_plan(diet_type, meal_count, allergies, dislikes):
    library = FALLBACK_MEAL_LIBRARY.get(diet_type or "Vegetarian", FALLBACK_MEAL_LIBRARY["Vegetarian"])
    avoid_terms = _parse_preference_list(allergies) | _parse_preference_list(dislikes)

    plan = []
    for index, meal_type in enumerate(_meal_slots_for_count(meal_count)):
        options = library.get(meal_type) or FALLBACK_MEAL_LIBRARY["Vegetarian"].get(meal_type, [])
        if not options:
            continue
        meal = _select_fallback_meal_option(options, avoid_terms, index)
        meal["meal_type"] = meal_type
        plan.append(meal)
    return plan

def _fallback_recipe_lookup(food_name):
    normalized = _normalize_lookup_key(food_name)
    nutrition = _fallback_nutrition_lookup(food_name)

    for candidate, recipe in FALLBACK_RECIPE_LIBRARY.items():
        if candidate == normalized or candidate in normalized or normalized in candidate:
            fallback_recipe = copy.deepcopy(recipe)
            fallback_recipe["nutrition"] = nutrition
            return fallback_recipe

    title = " ".join(word.capitalize() for word in normalized.split()) or "Simple Dish"
    return {
        "recipe_title": f"Simple {title}",
        "ingredients": [
            f"1 cup prepared {title.lower()}",
            "1 teaspoon oil or ghee",
            "1 small onion, chopped",
            "1 small tomato, chopped",
            "Salt and pepper to taste",
            "Fresh coriander or lemon for garnish",
        ],
        "instructions": [
            "Heat oil in a pan and saute the onion until soft.",
            f"Add the {title.lower()} base and mix in the tomato with simple seasonings.",
            "Cook for 4-5 minutes until everything is heated through and well combined.",
            "Finish with coriander or lemon and serve warm.",
        ],
        "nutrition": nutrition,
    }

def _bmi_category(height_cm, weight_kg):
    try:
        height_m = float(height_cm) / 100
        weight_kg = float(weight_kg)
        bmi = weight_kg / (height_m ** 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None

    if bmi < 18.5:
        return round(bmi, 1), "Underweight"
    if bmi < 25:
        return round(bmi, 1), "Normal weight"
    if bmi < 30:
        return round(bmi, 1), "Overweight"
    return round(bmi, 1), "Higher than recommended"

def _fallback_health_insights(form_data):
    bmi_value, bmi_category = _bmi_category(form_data.get("height"), form_data.get("weight"))
    bmi_category = bmi_category or "Needs review"
    bmi_comment_map = {
        "Underweight": "Your body may need a little more energy and protein support. Regular meals and balanced snacks can help.",
        "Normal weight": "Your current BMI is in a healthy range. Staying consistent with meals, sleep, and movement will help.",
        "Overweight": "A few routine changes can help improve balance over time. Focus on sleep, hydration, and regular activity.",
        "Higher than recommended": "Small daily habit changes can make a meaningful difference. Build a steady routine rather than chasing quick fixes.",
        "Needs review": "Some measurements were incomplete, so the BMI could not be fully assessed. The rest of the habits still give useful clues.",
    }

    labels = {
        "meals_per_day": "Meals per day",
        "fruit_veg_intake": "Fruits and vegetables",
        "junk_food_intake": "Junk food and soft drinks",
        "water_intake": "Water intake",
        "sleep_hours": "Sleep hours",
        "physical_activity": "Physical activity",
    }

    positive_points = []
    if form_data.get("sleep_hours") == "High":
        positive_points.append("You are getting strong sleep support, which helps recovery, focus, and healthy growth.")
    if form_data.get("physical_activity") in {"Medium", "High"}:
        positive_points.append("Your activity level supports stamina, strength, and overall health.")
    if form_data.get("fruit_veg_intake") in {"Medium", "High"}:
        positive_points.append("Your fruit and vegetable intake adds useful vitamins, minerals, and fiber to your routine.")
    if form_data.get("junk_food_intake") == "Low":
        positive_points.append("Keeping junk food and soft drinks low is a strong habit for long-term health.")
    if form_data.get("water_intake") in {"Medium", "High"}:
        positive_points.append("Your hydration habits are helping your energy and concentration stay steadier through the day.")
    if len(positive_points) < 2:
        positive_points.append("You have already started building awareness around your daily routine, which is a strong first step.")

    areas_for_improvement = []
    recommendation_map = {
        "meals_per_day": "Try to avoid skipping meals. A steady meal pattern helps with energy, focus, and hunger control.",
        "fruit_veg_intake": "Add one extra fruit or vegetable to a main meal or snack each day.",
        "junk_food_intake": "Keep packaged snacks and sugary drinks for occasional treats, not daily habits.",
        "water_intake": "Keep a water bottle nearby and aim to drink at regular times during the day.",
        "sleep_hours": "Try to keep a regular bedtime and reduce screens before sleep.",
        "physical_activity": "Add a brisk walk, sport, stretching, or play time each day to build consistency.",
    }

    for key, label in labels.items():
        value = form_data.get(key)
        needs_help = (key == "junk_food_intake" and value != "Low") or (key != "junk_food_intake" and value == "Low")
        if needs_help:
            areas_for_improvement.append({"point": label, "recommendation": recommendation_map[key]})

    if not areas_for_improvement:
        areas_for_improvement.append({
            "point": "Consistency",
            "recommendation": "Your habits look balanced overall. Keep following the same routine and review it every few weeks.",
        })

    overall_summary = "Your current routine shows a solid base, with a few practical areas to keep improving."
    if bmi_category == "Underweight":
        overall_summary = "You may benefit from a little more meal structure and energy intake, but there are clear habits we can build on."
    elif bmi_category == "Normal weight":
        overall_summary = "Your measurements and routines suggest a healthy base. Keeping the good habits consistent will help a lot."
    elif bmi_category in {"Overweight", "Higher than recommended"}:
        overall_summary = "Your report suggests that a few steady routine changes could improve your overall health over time."

    if bmi_value is not None:
        overall_summary += f" Your current BMI is about {bmi_value}."

    return {
        "overall_summary": overall_summary,
        "bmi_analysis": {
            "category": bmi_category,
            "comment": bmi_comment_map[bmi_category],
        },
        "positive_points": positive_points[:3],
        "areas_for_improvement": areas_for_improvement[:3],
    }

def _call_gemini_json(prompt, *, generation_config, log_label):
    started_at = time.perf_counter()
    try:
        try:
            result = _call_gemini_rest_json(prompt, generation_config=generation_config)
        except Exception as rest_exc:
            logger.warning("%s REST path failed, trying SDK fallback: %s", log_label, rest_exc)
            response = _build_gemini_model().generate_content(
                prompt,
                generation_config=generation_config,
                request_options=genai.types.RequestOptions(timeout=_gemini_timeout_seconds()),
            )

            if not response.parts:
                reason = "Unknown"
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason = response.prompt_feedback.block_reason.name
                raise ValueError(f"AI response was blocked due to safety concerns: {reason}.")

            result = _parse_ai_json_response(response.text)

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        logger.info("%s completed in %sms", log_label, elapsed_ms)
        return result
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        logger.exception("%s failed in %sms", log_label, elapsed_ms)
        raise

@lru_cache(maxsize=128)
def _cached_ai_nutrition_lookup(food_name):
    prompt = (
        f'Return only JSON for 100 grams of "{food_name.title()}". '
        'Use this schema: {"calories": number, "protein": number, "carbs": number, "fats": number}. '
        "Use numbers only."
    )
    result = _call_gemini_json(
        prompt,
        generation_config=_nutrition_generation_config(),
        log_label=f'Nutrition lookup for "{food_name}"',
    )
    return _coerce_nutrition_payload(result)

@lru_cache(maxsize=64)
def _cached_ai_recipe_lookup(food_name):
    prompt = f"""
    Return only JSON for a simple, healthy recipe for "{food_name.title()}" for a student in India.
    Use this schema:
    {{
      "recipe_title": string,
      "ingredients": [string],
      "instructions": [string],
      "nutrition": {{
        "calories": number,
        "protein": number,
        "carbs": number,
        "fats": number
      }}
    }}
    Keep the recipe concise and practical.
    """
    result = _call_gemini_json(
        prompt,
        generation_config=_recipe_generation_config(),
        log_label=f'Recipe lookup for "{food_name}"',
    )
    return _coerce_recipe_payload(result)

try:
    if not _load_ai_api_key():
        raise ValueError("GOOGLE_API_KEY is not set.")
    logger.info("Google AI model ready: %s", _gemini_model_name())
except Exception as e:
    _reset_gemini_model_cache()
    logger.warning("Error initializing Google AI model: %s. The API key might be missing or invalid.", e)

main = Blueprint('main', __name__)

def _max_student_dob(today=None):
    today = today or date.today()
    try:
        return today.replace(year=today.year - 5)
    except ValueError:
        return today.replace(year=today.year - 5, day=28)

def _parse_form_date(field_name):
    value = request.form.get(field_name, '')
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _school_scope_id():
    return getattr(current_user, 'school_scope_id', None) or getattr(current_user, 'id', None)


def _active_school_account():
    school_scope_id = _school_scope_id()
    if not school_scope_id:
        return None
    return db.session.get(User, school_scope_id)


def _school_food_query(school_scope_id=None):
    resolved_school_id = school_scope_id or _school_scope_id()
    if not resolved_school_id:
        return Food.query.filter(Food.school_id.is_(None))
    return Food.query.filter(or_(Food.school_id == resolved_school_id, Food.school_id.is_(None)))


def _school_food_ids(food_ids, school_scope_id=None):
    if not food_ids:
        return set()

    return {
        food_id
        for (food_id,) in _school_food_query(school_scope_id)
        .with_entities(Food.id)
        .filter(Food.id.in_(set(food_ids)))
        .all()
    }


def _log_ai_usage(user, feature, status, *, request_units=0, latency_ms=None, details=None):
    try:
        add_ai_usage_log(
            user,
            feature,
            status=status,
            request_units=request_units,
            latency_ms=latency_ms,
            details=details,
        )
        db.session.commit()
    except Exception:
        _rollback_session()
        logger.exception("Failed to persist AI usage log for feature=%s status=%s", feature, status)


def _rollback_session():
    try:
        db.session.rollback()
    except Exception:
        logger.exception("Database rollback failed.")


def _commit_session(action_label):
    try:
        db.session.commit()
        logger.info("%s completed successfully", action_label)
        return True
    except SQLAlchemyError:
        _rollback_session()
        logger.exception("%s failed during database commit", action_label)
        return False


def _parse_positive_int(value, field_label, minimum=1):
    try:
        parsed_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} must be a whole number.") from exc

    if parsed_value < minimum:
        raise ValueError(f"{field_label} must be at least {minimum}.")
    return parsed_value


def _parse_food_ids(form_values):
    parsed_ids = []
    for value in form_values:
        if value in {None, ''}:
            continue
        parsed_ids.append(_parse_positive_int(value, "Food selection"))
    return parsed_ids


def _validate_food_ids(food_ids, school_scope_id=None):
    if not food_ids:
        return True

    existing_ids = _school_food_ids(food_ids, school_scope_id)
    missing_ids = [food_id for food_id in food_ids if food_id not in existing_ids]
    if missing_ids:
        logger.warning("Rejected meal plan submission with missing food ids: %s", missing_ids)
        return False
    return True


def _group_plan_meals(plan):
    grouped_meals = {
        'Breakfast': [],
        'Lunch': [],
        'Dinner': [],
    }
    if not plan or not getattr(plan, 'items', None):
        return grouped_meals

    for item in plan.items:
        food = getattr(item, 'food', None)
        if food is None:
            logger.warning(
                "Meal plan item %s in plan %s references a missing food record.",
                getattr(item, 'id', None),
                getattr(plan, 'id', None),
            )
            continue

        grouped_meals.setdefault(item.meal_type, []).append({
            'name': food.name,
            'calories': food.calories,
        })

    return grouped_meals


def _meal_plan_history_payload(plan):
    grouped_meals = _group_plan_meals(plan)
    payload = {}
    for meal_type, items in grouped_meals.items():
        payload[meal_type] = [
            f"{item['name']} ({item['calories']} kcal)"
            for item in items
        ]
    return payload


def _default_school_summary():
    return {
        'student_count': 0,
        'food_count': 0,
        'upcoming_plan_count': 0,
        'today_plan_item_count': 0,
        'attendance_marked_today': 0,
        'students_served_today': 0,
        'attendance_completion_percent': 0,
        'attendance_enabled': False,
    }


def _empty_school_dashboard_context(today):
    return {
        'user': current_user,
        'school_account': _active_school_account(),
        'all_foods': [],
        'upcoming_plans': [],
        'today': today,
        'max_student_dob': _max_student_dob(today),
        'students': [],
        'students_for_attendance': [],
        'school_summary': _default_school_summary(),
        'attendance_enabled': False,
    }


def _empty_student_dashboard_context(today):
    return {
        'user': current_user,
        'today': today,
        'todays_plan': None,
        'daily_nutrition': {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
        'recommended_values': {'calories': 2000, 'protein': 50, 'carbs': 300, 'fats': 70},
        'nutrition_percentages': {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
        'meal_type_totals': {
            'Breakfast': {'calories': 0},
            'Lunch': {'calories': 0},
            'Dinner': {'calories': 0},
        },
        'weekly_nutrition': [
            {'day': (today - timedelta(days=i)).strftime('%a'), 'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0}
            for i in range(6, -1, -1)
        ],
        'weekly_averages': {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
        'attendance_percentage': 0,
        'recent_absences': [],
    }


def _empty_insights_context(today):
    return {
        'total_students': 0,
        'attendance_today_percent': 0,
        'avg_weekly_attendance_percent': 0,
        'total_meals_this_month': 0,
        'attendance_labels': [(today - timedelta(days=i)).strftime('%a %d') for i in range(6, -1, -1)],
        'attendance_data': [0] * 7,
        'meal_distribution_data': [0, 0, 0],
        'nutrition_compliance_labels': [],
        'nutrition_compliance_data': [],
        'health_impact_data': [
            {'metric': 'BMI Improvement %', 'current_value': 'N/A', 'change': 'N/A'},
            {'metric': 'Attendance Rise', 'current_value': '0%', 'change': 'N/A'},
        ],
        'class_insights': [],
    }


def _get_school_owned_student_user(user_id):
    student_user = db.session.get(User, user_id)
    if student_user is None:
        flash('Student account not found.', 'danger')
        return None, None

    student_detail = getattr(student_user, 'student_detail', None)
    if student_detail is None:
        logger.warning(
            "School %s attempted to manage user %s without an attached student detail record.",
            getattr(current_user, 'id', None),
            user_id,
        )
        flash('Student record is incomplete or missing.', 'danger')
        return None, None

    if student_detail.school_id != _school_scope_id():
        flash('You do not have permission to manage this student.', 'danger')
        return None, None

    return student_user, student_detail


def _student_detail_or_logout():
    student_detail = getattr(current_user, 'portal_student_detail', None)
    if student_detail:
        return student_detail

    logger.warning("Student user %s is missing a student_detail record. Logging out.", getattr(current_user, 'id', None))
    flash('Student details not found.', 'danger')
    logout_user()
    return None


@main.route('/health')
def health():
    database_status = 'ok'
    status_code = 200
    try:
        db.session.execute(text('SELECT 1'))
    except Exception as exc:
        db.session.rollback()
        database_status = 'degraded'
        status_code = 503
        logger.warning("Health check database probe failed: %s", exc)

    payload = {
        'status': 'ok',
        'database': database_status,
        'service': 'nutrify',
        'environment': os.environ.get('APP_ENV', os.environ.get('FLASK_ENV', 'development')).lower(),
        'release': os.environ.get('RENDER_GIT_COMMIT', 'local'),
    }
    if database_status != 'ok':
        payload['status'] = 'degraded'

    return jsonify(payload), status_code

# --- HELPER FUNCTION (from File 2) ---
def _get_student_nutrition_data(student_detail, target_date):
    """A helper function to calculate all nutrition data for a student on a specific date."""
    if not student_detail:
        logger.warning("Nutrition data requested without a student detail for date %s", target_date)
        return {
            "meal_plan": None,
            "daily_nutrition": {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
            "recommended_values": {'calories': 2000, 'protein': 50, 'carbs': 300, 'fats': 70},
            "nutrition_percentages": {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
            "meal_type_totals": {
                'Breakfast': {'calories': 0},
                'Lunch': {'calories': 0},
                'Dinner': {'calories': 0}
            },
            "weekly_nutrition": [
                {'day': (target_date - timedelta(days=i)).strftime('%a'), 'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0}
                for i in range(6, -1, -1)
            ],
            "weekly_averages": {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0},
        }

    school_id = student_detail.school_id
    
    # 1. Daily Data
    meal_plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
        school_id=school_id,
        plan_date=target_date,
    ).first()
    attendance = Attendance.query.filter_by(student_id=student_detail.id, attendance_date=target_date).first()
    
    daily_nutrition = {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0}
    meal_type_totals = {
        'Breakfast': {'calories': 0},
        'Lunch': {'calories': 0},
        'Dinner': {'calories': 0}
    }

    if meal_plan and attendance:
        for item in meal_plan.items:
            food = getattr(item, 'food', None)
            if food is None:
                logger.warning(
                    "Skipping meal plan item %s for student %s because the food record is missing.",
                    getattr(item, 'id', None),
                    getattr(student_detail, 'id', None),
                )
                continue
            ate_meal = False
            if item.meal_type == 'Breakfast' and attendance.ate_breakfast: ate_meal = True
            elif item.meal_type == 'Lunch' and attendance.ate_lunch: ate_meal = True
            elif item.meal_type == 'Dinner' and attendance.ate_dinner: ate_meal = True
            
            if ate_meal:
                daily_nutrition['calories'] += food.calories
                daily_nutrition['protein'] += food.protein
                daily_nutrition['carbs'] += food.carbs
                daily_nutrition['fats'] += food.fats
                if item.meal_type in meal_type_totals:
                    meal_type_totals[item.meal_type]['calories'] += food.calories

    # 2. Recommended Values (using simple estimates)
    recommended_values = {'calories': 2000, 'protein': 50, 'carbs': 300, 'fats': 70}

    # 3. Nutrition Percentages
    nutrition_percentages = {
        key: min(100, round((daily_nutrition[key] / recommended_values[key]) * 100)) if recommended_values[key] > 0 else 0
        for key in recommended_values
    }

    # 4. Weekly Data
    weekly_nutrition = []
    weekly_totals = {'calories': 0, 'protein': 0, 'carbs': 0, 'fats': 0}
    day_count = 0
    
    for i in range(7):
        day = target_date - timedelta(days=i)
        day_plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
            school_id=school_id,
            plan_date=day,
        ).first()
        day_attendance = Attendance.query.filter_by(student_id=student_detail.id, attendance_date=day).first()
        
        day_calories, day_protein, day_carbs, day_fats = 0, 0, 0, 0
        if day_plan and day_attendance and day_attendance.was_present:
            day_count += 1
            for item in day_plan.items:
                food = getattr(item, 'food', None)
                if food is None:
                    logger.warning(
                        "Skipping historical meal plan item %s for student %s because the food record is missing.",
                        getattr(item, 'id', None),
                        getattr(student_detail, 'id', None),
                    )
                    continue
                if (item.meal_type == 'Breakfast' and day_attendance.ate_breakfast) or \
                   (item.meal_type == 'Lunch' and day_attendance.ate_lunch) or \
                   (item.meal_type == 'Dinner' and day_attendance.ate_dinner):
                    day_calories += food.calories
                    day_protein += food.protein
                    day_carbs += food.carbs
                    day_fats += food.fats
        
        weekly_totals['calories'] += day_calories
        weekly_totals['protein'] += day_protein
        weekly_totals['carbs'] += day_carbs
        weekly_totals['fats'] += day_fats
        
        weekly_nutrition.insert(0, {'day': day.strftime('%a'), 'calories': round(day_calories), 'protein': round(day_protein), 'carbs': round(day_carbs), 'fats': round(day_fats)})

    weekly_averages = {key: round(weekly_totals[key] / day_count) if day_count > 0 else 0 for key in weekly_totals}

    return {
        "meal_plan": meal_plan, "daily_nutrition": {key: round(value) for key, value in daily_nutrition.items()},
        "recommended_values": recommended_values, "nutrition_percentages": nutrition_percentages,
        "meal_type_totals": meal_type_totals, "weekly_nutrition": weekly_nutrition, "weekly_averages": weekly_averages
    }

# --- AUTHENTICATION ROUTES (with enhanced feedback from File 1) ---
@main.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return render_template('index.html')

@main.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not username or not password:
            flash('Please enter both username and password.', 'danger')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if getattr(user, 'is_deleted', False) or not getattr(user, 'is_active', True):
                flash('Your account is not active. Please contact support.', 'danger')
                return render_template('login.html')
            if getattr(user, 'is_locked', False):
                flash('Your account is locked. Please contact support.', 'danger')
                return render_template('login.html')
            try:
                if user.uses_legacy_password_hash:
                    user.set_password(password)
                user.last_login_at = utcnow()
                add_audit_log(
                    'login',
                    'user',
                    entity_id=user.id,
                    actor_user=user,
                    details={'username': user.username, 'role': user.normalized_role},
                )
                if not _commit_session(f"Login bookkeeping for user_id={user.id}"):
                    logger.warning('Continuing login without bookkeeping commit for user_id=%s', user.id)
            except Exception:
                _rollback_session()
                current_app.logger.warning(
                    'Unable to finalize login bookkeeping for user_id=%s',
                    user.id,
                    exc_info=True,
                )
            login_user(user)
            establish_session(user)
            logger.info("User %s logged in successfully with role=%s", user.username, user.role)
            flash('Logged in successfully!', 'success')
            if getattr(user, 'force_password_reset', False):
                return redirect(url_for('platform.change_password'))
            return redirect(url_for('main.dashboard'))
        else:
            logger.info("Failed login attempt for username=%s", username)
            add_audit_log(
                'login_failed',
                'user',
                entity_id=username or None,
                actor_user=None,
                school_id=None,
                status='failed',
                details={'username': username},
            )
            _commit_session(f"Failed login audit username={username or 'blank'}")
            flash('Invalid username or password.', 'danger')
    return render_template('login.html')

@main.route('/logout', methods=['POST'])
@login_required
def logout():
    try:
        add_audit_log('logout', 'user', entity_id=current_user.id, details={'username': current_user.username})
        _commit_session(f"Logout audit user_id={current_user.id}")
    except Exception:
        _rollback_session()
        logger.exception("Failed to persist logout audit for user_id=%s", getattr(current_user, 'id', None))
    logger.info("User %s logged out", getattr(current_user, 'username', None))
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.login'))

# --- MAIN DASHBOARD ROUTE (using advanced logic from File 2) ---
@main.route('/dashboard')
@login_required
def dashboard():
    today = date.today()
    if current_user.has_role(User.ROLE_MASTER_ADMIN):
        return redirect(url_for('platform.platform_dashboard'))

    if current_user.has_role(User.ROLE_SCHOOL_ADMIN):
        school_scope_id = _school_scope_id()
        try:
            max_student_dob = _max_student_dob(today)
            all_foods = _school_food_query(school_scope_id).order_by(Food.name).all()
            backdated_plans = MealPlan.query.filter(MealPlan.school_id == school_scope_id, MealPlan.plan_date < today).all()
            for plan in backdated_plans:
                plan.soft_delete()
                add_audit_log('soft_delete', 'meal_plan', entity_id=plan.id, details={'reason': 'backdated_cleanup'})
            if backdated_plans and not _commit_session(f"Backdated meal plan cleanup for school_id={current_user.id}"):
                flash('Some expired meal plans could not be cleaned up automatically.', 'warning')

            upcoming_plans = MealPlan.query.filter(MealPlan.school_id == school_scope_id, MealPlan.plan_date >= today).order_by(MealPlan.plan_date.desc()).all()
            student_details = StudentDetail.query.filter_by(school_id=school_scope_id).order_by(StudentDetail.roll_no).all()
            students_for_attendance = [{'id': detail.id, 'name': detail.full_name, 'class': f"Grade {detail.grade} - {detail.section}", 'roll_no': detail.roll_no} for detail in student_details]

            student_ids = [detail.id for detail in student_details]
            today_plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
                school_id=school_scope_id,
                plan_date=today,
            ).first()
            today_attendance_records = Attendance.query.filter(
                Attendance.student_id.in_(student_ids),
                Attendance.attendance_date == today
            ).all() if student_ids else []

            attendance_marked_today = len(today_attendance_records)
            students_served_today = sum(1 for record in today_attendance_records if record.was_present)
            attendance_completion_percent = round((attendance_marked_today / len(student_details)) * 100, 1) if student_details else 0
            attendance_enabled = bool(today_plan and today_plan.items)
            school_summary = {
                'student_count': len(student_details),
                'food_count': len(all_foods),
                'upcoming_plan_count': len(upcoming_plans),
                'today_plan_item_count': len(today_plan.items) if today_plan and today_plan.items else 0,
                'attendance_marked_today': attendance_marked_today,
                'students_served_today': students_served_today,
                'attendance_completion_percent': attendance_completion_percent,
                'attendance_enabled': attendance_enabled,
            }

            return render_template(
                'dashboard_school.html',
                user=current_user,
                school_account=_active_school_account(),
                all_foods=all_foods,
                upcoming_plans=upcoming_plans,
                today=today,
                max_student_dob=max_student_dob,
                students=student_details,
                students_for_attendance=students_for_attendance,
                school_summary=school_summary,
                attendance_enabled=attendance_enabled
            )
        except Exception:
            _rollback_session()
            logger.exception("Failed to load school dashboard for user_id=%s", current_user.id)
            flash('Some dashboard data could not be loaded right now.', 'warning')
            return render_template('dashboard_school.html', **_empty_school_dashboard_context(today))

    if current_user.has_role(User.ROLE_USER):
        student_detail = _student_detail_or_logout()
        if not student_detail:
            return redirect(url_for('main.login'))

        try:
            nutrition_data = _get_student_nutrition_data(student_detail, today)
            attendance_history = Attendance.query.filter_by(student_id=student_detail.id).order_by(Attendance.attendance_date.desc()).all()
            total_records = len(attendance_history)
            present_records = sum(1 for r in attendance_history if r.was_present)
            attendance_percentage = round((present_records / total_records) * 100, 1) if total_records > 0 else 100
            recent_absences = [r for r in attendance_history if not r.was_present][:5]

            return render_template('dashboard_student.html', user=current_user, student=student_detail, today=today, todays_plan=nutrition_data['meal_plan'],
                                   daily_nutrition=nutrition_data['daily_nutrition'], recommended_values=nutrition_data['recommended_values'],
                                   nutrition_percentages=nutrition_data['nutrition_percentages'], meal_type_totals=nutrition_data['meal_type_totals'],
                                   weekly_nutrition=nutrition_data['weekly_nutrition'], weekly_averages=nutrition_data['weekly_averages'],
                                   attendance_percentage=attendance_percentage, recent_absences=recent_absences)
        except Exception:
            _rollback_session()
            logger.exception("Failed to load student dashboard for user_id=%s", current_user.id)
            flash('Some dashboard data could not be loaded right now.', 'warning')
            return render_template('dashboard_student.html', student=student_detail, **_empty_student_dashboard_context(today))

    logger.warning("User %s has unsupported role=%s for dashboard access", current_user.username, current_user.role)
    flash('Your account role is not configured for a dashboard yet.', 'danger')
    logout_user()
    return redirect(url_for('main.login'))

# --- STUDENT MANAGEMENT (full CRUD from File 2) ---
@main.route('/add-student', methods=['POST'])
@login_required
def add_student():
    if not current_user.can_manage_students_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    school_scope_id = _school_scope_id()
    
    username = request.form.get('username', '').strip()
    full_name = request.form.get('full_name', '').strip()
    section = request.form.get('section', '').strip()
    sex = request.form.get('sex')
    if User.query.filter_by(username=username).first():
        flash(f'Login username "{username}" already exists.', 'danger')
        return redirect(url_for('main.dashboard'))

    password = request.form.get('password', '')
    student_dob = _parse_form_date('dob')
    max_student_dob = _max_student_dob()
    height_cm = request.form.get('height_cm', type=float)
    weight_kg = request.form.get('weight_kg', type=float)

    if not username or len(password) < 6:
        flash('Please provide a username and an initial password of at least 6 characters.', 'danger')
        return redirect(url_for('main.dashboard'))
    if not full_name or not section or sex not in {'Male', 'Female'}:
        flash('Please provide the required student profile details before saving.', 'danger')
        return redirect(url_for('main.dashboard'))
    if not student_dob or student_dob > max_student_dob:
        flash('Students must be at least 5 years old. Please choose an earlier date of birth.', 'danger')
        return redirect(url_for('main.dashboard'))
    if (height_cm is not None and height_cm <= 0) or (weight_kg is not None and weight_kg <= 0):
        flash('Height and weight must be positive numbers.', 'danger')
        return redirect(url_for('main.dashboard'))

    try:
        roll_no = _parse_positive_int(request.form.get('roll_no'), 'Roll number')
        grade = _parse_positive_int(request.form.get('grade'), 'Grade')
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('main.dashboard'))
    
    try:
        new_user = User(username=username, role=User.ROLE_USER, school_id=school_scope_id)
        new_user.set_password(password)
        student_details = StudentDetail(full_name=full_name, roll_no=roll_no, dob=student_dob, sex=sex, grade=grade, section=section, school_id=school_scope_id)
        new_user.student_detail = student_details
        db.session.add(new_user)
        
        if height_cm and weight_kg:
            initial_metric = HealthMetric(student_detail=student_details, height_cm=height_cm, weight_kg=weight_kg, record_date=date.today())
            db.session.add(initial_metric)
        add_audit_log('create', 'student', entity_id=username, details={'school_id': school_scope_id, 'full_name': full_name})
        
        if not _commit_session(f"Add student username={username} school_id={school_scope_id}"):
            flash('An error occurred while adding the student. Please try again.', 'danger')
            return redirect(url_for('main.dashboard'))

        broadcast_school_notification(
            school_scope_id,
            'Student account added',
            f'{student_details.full_name} was added to the roster.',
            category='success',
            link=url_for('main.dashboard'),
        )
        _commit_session(f"Student add notifications school_id={school_scope_id}")
        flash(f'Student "{student_details.full_name}" created successfully!', 'success')
    except Exception as e:
        _rollback_session()
        logger.exception("Unexpected error while adding student username=%s for school_id=%s", username, school_scope_id)
        flash(f'An error occurred while adding the student: {e}', 'danger')
    return redirect(url_for('main.dashboard'))

@main.route('/delete-student/<int:user_id>', methods=['POST'])
@login_required
def delete_student(user_id):
    if not current_user.can_manage_students_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))

    user_to_delete, student_detail = _get_school_owned_student_user(user_id)
    if user_to_delete is None or student_detail is None:
        return redirect(url_for('main.dashboard'))
    
    student_name = student_detail.full_name
    user_to_delete.soft_delete()
    student_detail.soft_delete()
    add_audit_log('soft_delete', 'student', entity_id=user_id, details={'school_id': _school_scope_id(), 'full_name': student_name})
    if not _commit_session(f"Delete student user_id={user_id} school_id={_school_scope_id()}"):
        flash('The student could not be deleted right now. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    flash(f'Student "{student_name}" has been deleted successfully.', 'success')
    return redirect(url_for('main.dashboard'))

@main.route('/edit-student/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_student(user_id):
    if not current_user.can_manage_students_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))

    user_to_edit, student_detail = _get_school_owned_student_user(user_id)
    if user_to_edit is None or student_detail is None:
        return redirect(url_for('main.dashboard'))

    max_student_dob = _max_student_dob()
    if request.method == 'POST':
        student_dob = _parse_form_date('dob')
        if not student_dob or student_dob > max_student_dob:
            flash('Students must be at least 5 years old. Please choose an earlier date of birth.', 'danger')
            return redirect(url_for('main.edit_student', user_id=user_id))

        try:
            full_name = request.form.get('full_name', '').strip()
            section = request.form.get('section', '').strip()
            sex = request.form.get('sex')
            if not full_name or not section or sex not in {'Male', 'Female'}:
                raise ValueError('Please provide the required student profile details before saving.')

            student_detail.full_name = full_name
            student_detail.roll_no = _parse_positive_int(request.form.get('roll_no'), 'Roll number')
            student_detail.dob = student_dob
            student_detail.sex = sex
            student_detail.grade = _parse_positive_int(request.form.get('grade'), 'Grade')
            student_detail.section = section
            
            new_password = request.form.get('password')
            if new_password:
                if len(new_password) < 6:
                    flash('New password must be at least 6 characters.', 'danger')
                    return redirect(url_for('main.edit_student', user_id=user_id))
                user_to_edit.set_password(new_password)
            
            add_audit_log('update', 'student', entity_id=user_id, details={'school_id': _school_scope_id(), 'full_name': student_detail.full_name})
            if not _commit_session(f"Edit student user_id={user_id} school_id={_school_scope_id()}"):
                flash('The student details could not be updated right now. Please try again.', 'danger')
                return redirect(url_for('main.edit_student', user_id=user_id))

            flash(f'Details for "{student_detail.full_name}" have been updated!', 'success')
            return redirect(url_for('main.dashboard'))
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('main.edit_student', user_id=user_id))
        except Exception:
            _rollback_session()
            logger.exception("Unexpected error while editing student user_id=%s for school_id=%s", user_id, current_user.id)
            flash('An unexpected error occurred while updating the student.', 'danger')
            return redirect(url_for('main.edit_student', user_id=user_id))

    return render_template('edit_student.html', user=user_to_edit, max_student_dob=max_student_dob)

# --- MEAL PLAN MANAGEMENT (full CRUD from File 2) ---
@main.route('/create-meal-plan', methods=['POST'])
@login_required
def create_meal_plan():
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    school_scope_id = _school_scope_id()
    plan_date = _parse_form_date('plan_date')
    if not plan_date:
        flash('Please choose a valid date for the meal plan.', 'danger')
        return redirect(url_for('main.dashboard'))
    if plan_date < date.today():
        flash('Meal plans can only be created for today or a future date.', 'danger')
        return redirect(url_for('main.dashboard'))
    if MealPlan.query.filter_by(school_id=school_scope_id, plan_date=plan_date).first():
        flash(f'A meal plan for {plan_date.strftime("%d %B, %Y")} already exists.', 'warning')
        return redirect(url_for('main.dashboard'))

    meal_types = ['breakfast', 'lunch', 'dinner'] 
    selected_food_ids = {}
    all_selected_food_ids = []
    try:
        for meal_type in meal_types:
            food_ids = _parse_food_ids(request.form.getlist(f'{meal_type}_foods'))
            selected_food_ids[meal_type] = food_ids
            all_selected_food_ids.extend(food_ids)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('main.dashboard'))

    if not _validate_food_ids(all_selected_food_ids, school_scope_id):
        flash('One or more selected food items are no longer available. Please refresh and try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    approved_immediately = current_user.can_approve_workflows_effective
    new_plan = MealPlan(
        school_id=school_scope_id,
        plan_date=plan_date,
        status='approved' if approved_immediately else 'pending',
        created_by_user_id=current_user.id,
        approved_by_user_id=current_user.id if approved_immediately else None,
        approved_at=utcnow() if approved_immediately else None,
    )
    db.session.add(new_plan)
    items_added = False
    for meal_type in meal_types:
        food_ids = selected_food_ids[meal_type]
        if food_ids:
            items_added = True
        for food_id in food_ids:
            item = MealPlanItem(plan=new_plan, food_id=food_id, meal_type=meal_type.capitalize())
            db.session.add(item)
    if not items_added:
        _rollback_session()
        flash('Cannot create an empty meal plan. Please select at least one food item.', 'danger')
        return redirect(url_for('main.dashboard'))

    db.session.flush()
    if not approved_immediately:
        db.session.add(
            ApprovalRequest(
                school_id=school_scope_id,
                request_type='meal_plan_approval',
                target_model='MealPlan',
                target_id=str(new_plan.id),
                requester_user_id=current_user.id,
                payload={'plan_date': plan_date.isoformat()},
            )
        )
    add_audit_log('create', 'meal_plan', entity_id=plan_date.isoformat(), details={'school_id': school_scope_id, 'status': new_plan.status})

    if not _commit_session(f"Create meal plan school_id={school_scope_id} plan_date={plan_date.isoformat()}"):
        flash('The meal plan could not be created right now. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    broadcast_school_notification(
        school_scope_id,
        'Meal plan updated',
        f'Meal plan for {plan_date.strftime("%d %B, %Y")} is now {new_plan.status}.',
        category='success' if approved_immediately else 'warning',
        link=url_for('main.dashboard'),
    )
    _commit_session(f"Meal plan notifications school_id={school_scope_id}")
    flash(
        f'Meal plan for {plan_date.strftime("%d %B, %Y")} {"created" if approved_immediately else "submitted for approval"}!',
        'success',
    )
    return redirect(url_for('main.dashboard'))

@main.route('/delete-meal-plan/<int:plan_id>', methods=['POST'])
@login_required
def delete_meal_plan(plan_id):
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    plan = MealPlan.query.get_or_404(plan_id)
    if plan.school_id != _school_scope_id():
        flash('You do not have permission to delete this plan.', 'danger')
        return redirect(url_for('main.dashboard'))
    plan.soft_delete()
    add_audit_log('soft_delete', 'meal_plan', entity_id=plan_id, details={'school_id': _school_scope_id(), 'plan_date': plan.plan_date.isoformat()})
    if not _commit_session(f"Delete meal plan plan_id={plan_id} school_id={_school_scope_id()}"):
        flash('The meal plan could not be deleted right now. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    broadcast_school_notification(
        _school_scope_id(),
        'Meal plan removed',
        f'Meal plan for {plan.plan_date.strftime("%d %b, %Y")} was archived.',
        category='warning',
        link=url_for('main.dashboard'),
    )
    _commit_session(f"Meal plan delete notifications school_id={_school_scope_id()}")
    flash(f'Meal plan for {plan.plan_date.strftime("%d %b, %Y")} has been deleted.', 'success')
    return redirect(url_for('main.dashboard'))

@main.route('/edit-meal-plan/<int:plan_id>', methods=['GET', 'POST'])
@login_required
def edit_meal_plan(plan_id):
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    plan = MealPlan.query.get_or_404(plan_id)
    if plan.school_id != _school_scope_id():
        flash('You do not have permission to edit this meal plan.', 'danger')
        return redirect(url_for('main.dashboard'))
    if plan.plan_date < date.today():
        plan.soft_delete()
        add_audit_log('soft_delete', 'meal_plan', entity_id=plan_id, details={'reason': 'backdated_edit_cleanup'})
        if not _commit_session(f"Delete backdated meal plan plan_id={plan_id} school_id={_school_scope_id()}"):
            flash('The outdated meal plan could not be removed right now. Please try again.', 'danger')
            return redirect(url_for('main.dashboard'))

        flash('Backdated meal plans are no longer available.', 'warning')
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        meal_types = ['breakfast', 'lunch', 'dinner']
        selected_food_ids = {}
        all_selected_food_ids = []
        try:
            for meal_type in meal_types:
                food_ids = _parse_food_ids(request.form.getlist(f'{meal_type}_foods'))
                selected_food_ids[meal_type] = food_ids
                all_selected_food_ids.extend(food_ids)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('main.edit_meal_plan', plan_id=plan_id))

        if not _validate_food_ids(all_selected_food_ids, _school_scope_id()):
            flash('One or more selected food items are no longer available. Please refresh and try again.', 'danger')
            return redirect(url_for('main.edit_meal_plan', plan_id=plan_id))

        MealPlanItem.query.filter_by(meal_plan_id=plan_id).delete()
        items_added = False
        for meal_type in meal_types:
            food_ids = selected_food_ids[meal_type]
            if food_ids:
                items_added = True
            for food_id in food_ids:
                item = MealPlanItem(plan=plan, food_id=food_id, meal_type=meal_type.capitalize())
                db.session.add(item)
        if not items_added:
            flash('Cannot save an empty meal plan.', 'danger')
            _rollback_session()
            return redirect(url_for('main.edit_meal_plan', plan_id=plan_id))

        plan.status = 'approved' if current_user.can_approve_workflows_effective else 'pending'
        plan.created_by_user_id = current_user.id
        plan.approved_by_user_id = current_user.id if current_user.can_approve_workflows_effective else None
        plan.approved_at = utcnow() if current_user.can_approve_workflows_effective else None
        add_audit_log('update', 'meal_plan', entity_id=plan_id, details={'school_id': _school_scope_id(), 'status': plan.status})
        if plan.status == 'pending':
            db.session.add(
                ApprovalRequest(
                    school_id=_school_scope_id(),
                    request_type='meal_plan_approval',
                    target_model='MealPlan',
                    target_id=str(plan.id),
                    requester_user_id=current_user.id,
                    payload={'plan_date': plan.plan_date.isoformat()},
                )
            )
        if not _commit_session(f"Edit meal plan plan_id={plan_id} school_id={_school_scope_id()}"):
            flash('The meal plan could not be updated right now. Please try again.', 'danger')
            return redirect(url_for('main.edit_meal_plan', plan_id=plan_id))

        broadcast_school_notification(
            _school_scope_id(),
            'Meal plan updated',
            f'Meal plan for {plan.plan_date.strftime("%d %B, %Y")} is now {plan.status}.',
            category='success' if plan.status == 'approved' else 'warning',
            link=url_for('main.dashboard'),
        )
        _commit_session(f"Meal plan edit notifications school_id={_school_scope_id()}")
        flash(f'Meal plan for {plan.plan_date.strftime("%d %B, %Y")} updated!', 'success')
        return redirect(url_for('main.dashboard'))

    all_foods = _school_food_query().order_by(Food.name).all()
    existing_food_ids = {'Breakfast': set(), 'Lunch': set(), 'Dinner': set()}
    for item in plan.items:
        if item.meal_type in existing_food_ids:
            existing_food_ids[item.meal_type].add(item.food_id)
    return render_template('edit_meal_plan.html', plan=plan, all_foods=all_foods, existing_food_ids=existing_food_ids)

# --- ATTENDANCE ROUTES (using detailed meal tracking from File 2) ---
@main.route('/save-attendance', methods=['POST'])
@login_required
def save_attendance():
    if not current_user.can_manage_attendance_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    school_scope_id = _school_scope_id()
    try:
        data = json.loads(request.form.get('attendance_data', '[]'))
    except json.JSONDecodeError:
        flash('Attendance data was invalid. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))
    if not isinstance(data, list):
        flash('Attendance data was invalid. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    attendance_date = _parse_form_date('attendance_date')
    if not attendance_date:
        flash('Please choose a valid attendance date.', 'danger')
        return redirect(url_for('main.dashboard'))
    if attendance_date != date.today():
        flash('Attendance can only be saved for today.', 'danger')
        return redirect(url_for('main.dashboard'))

    today_plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
        school_id=school_scope_id,
        plan_date=date.today(),
    ).first()
    if not today_plan or not today_plan.items:
        flash('Attendance can only be marked after a meal plan is created for today.', 'danger')
        return redirect(url_for('main.dashboard'))

    for student_data in data:
        if not isinstance(student_data, dict):
            logger.warning("Skipping malformed attendance payload entry for school_id=%s: %r", school_scope_id, student_data)
            continue
        student_detail = db.session.get(StudentDetail, student_data.get('id'))
        if not student_detail or student_detail.school_id != school_scope_id:
            continue
        record = Attendance.query.filter_by(student_id=student_detail.id, attendance_date=attendance_date).first()
        if not record:
            record = Attendance(student_id=student_detail.id, attendance_date=attendance_date)
            db.session.add(record)
        record.recorded_by_user_id = current_user.id
        record.approval_status = 'approved' if current_user.can_approve_workflows_effective else 'pending'
        meals = student_data.get('meals', {})
        if not isinstance(meals, dict):
            meals = {}
        record.ate_breakfast = bool(meals.get('breakfast'))
        record.ate_lunch = bool(meals.get('lunch'))
        record.ate_dinner = bool(meals.get('dinner'))
    if not current_user.can_approve_workflows_effective:
        db.session.add(
            ApprovalRequest(
                school_id=school_scope_id,
                request_type='attendance_approval',
                target_model='Attendance',
                target_id=attendance_date.isoformat(),
                requester_user_id=current_user.id,
                payload={'attendance_date': attendance_date.isoformat()},
            )
        )
    add_audit_log('update', 'attendance', entity_id=attendance_date.isoformat(), details={'school_id': school_scope_id})
    if not _commit_session(f"Save attendance school_id={school_scope_id} attendance_date={attendance_date.isoformat()}"):
        flash('Attendance could not be saved right now. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))

    broadcast_school_notification(
        school_scope_id,
        'Attendance updated',
        f'Attendance for {attendance_date.strftime("%d %B, %Y")} was saved.',
        category='success',
        link=url_for('main.dashboard'),
    )
    _commit_session(f"Attendance notifications school_id={school_scope_id}")
    flash(f'Meal attendance for {attendance_date.strftime("%d %B, %Y")} saved!', 'success')
    return redirect(url_for('main.dashboard'))

@main.route('/get-attendance/<iso_date>')
@login_required
def get_attendance_by_date(iso_date):
    if not current_user.can_manage_attendance_effective:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        target_date = date.fromisoformat(iso_date)
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    if target_date > date.today():
        return jsonify({'error': 'Future dates are not available'}), 400
    all_students = StudentDetail.query.filter_by(school_id=_school_scope_id()).order_by(StudentDetail.roll_no).all()
    student_ids = [student.id for student in all_students]
    attendance_records = Attendance.query.filter(
        Attendance.student_id.in_(student_ids),
        Attendance.attendance_date == target_date
    ).all() if student_ids else []
    records_by_student_id = {record.student_id: record for record in attendance_records}

    ate_something, ate_nothing, absent = [], [], []
    editor_students = []
    total_meal_checks = 0
    for student in all_students:
        record = records_by_student_id.get(student.id)
        student_data = {
            'id': student.id,
            'name': student.full_name,
            'class': f"Grade {student.grade} - {student.section}",
            'roll_no': student.roll_no
        }
        if record:
            student_data['meals'] = {
                'breakfast': record.ate_breakfast,
                'lunch': record.ate_lunch,
                'dinner': record.ate_dinner
            }
            total_meal_checks += int(record.ate_breakfast) + int(record.ate_lunch) + int(record.ate_dinner)
            if record.was_present:
                ate_something.append(student_data)
            else:
                ate_nothing.append(student_data)
        else:
            absent.append(student_data)

        editor_students.append({
            'id': student.id,
            'roll_no': student.roll_no,
            'name': student.full_name,
            'class': f"Grade {student.grade} - {student.section}",
            'meals': student_data.get('meals', {'breakfast': False, 'lunch': False, 'dinner': False})
        })

    summary = {
        'total_students': len(all_students),
        'ate_something': len(ate_something),
        'ate_nothing': len(ate_nothing),
        'absent': len(absent),
        'meal_checks': total_meal_checks,
    }
    return jsonify({
        'ate_something': ate_something,
        'ate_nothing': ate_nothing,
        'absent': absent,
        'editor_students': editor_students,
        'summary': summary
    })

# --- FOOD MENU CRUD (from File 2) ---
@main.route('/manage-foods', methods=['GET'])
@login_required
def manage_foods():
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    all_foods = _school_food_query().order_by(Food.name).all()
    return render_template('manage_foods.html', foods=all_foods)

# In backend/app/routes.py

@main.route('/add-food', methods=['POST'])
@login_required
def add_food():
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    school_scope_id = _school_scope_id()

    name = request.form.get('name', '').strip()
    if not name:
        flash('Food name cannot be empty.', 'danger')
        return redirect(url_for('main.manage_foods'))

    # Check if a food with this name already exists to prevent duplicates
    existing_food = _school_food_query(school_scope_id).filter_by(name=name).first()
    if existing_food:
        flash(f'Food item "{name}" already exists. Please use a different name or edit the existing one.', 'danger')
        return redirect(url_for('main.manage_foods'))

    try:
        calories = float(request.form.get('calories'))
        protein = float(request.form.get('protein'))
        carbs = float(request.form.get('carbs'))
        fats = float(request.form.get('fats'))
        if min(calories, protein, carbs, fats) < 0:
            raise ValueError('Nutrition values cannot be negative.')
        new_food = Food(
            name=name,
            calories=calories,
            protein=protein,
            carbs=carbs,
            fats=fats,
            school_id=school_scope_id,
            created_by_user_id=current_user.id,
        )
        db.session.add(new_food)
        add_audit_log('create', 'food', entity_id=name, details={'school_id': school_scope_id})
        if not _commit_session(f"Add food name={name} school_id={school_scope_id}"):
            flash('An error occurred while adding the food. Please try again.', 'danger')
            return redirect(url_for('main.manage_foods'))

        broadcast_school_notification(
            school_scope_id,
            'Food menu updated',
            f'{new_food.name} was added to the school food catalog.',
            category='success',
            link=url_for('main.manage_foods'),
        )
        _commit_session(f"Food add notifications school_id={school_scope_id}")
        flash(f'Food item "{new_food.name}" was added successfully!', 'success')
    except Exception as e:
        _rollback_session()
        logger.exception("Unexpected error while adding food name=%s for school_id=%s", name, school_scope_id)
        flash(f'An error occurred while adding the food: {e}', 'danger')
    
    return redirect(url_for('main.manage_foods'))

@main.route('/edit-food/<int:food_id>', methods=['GET', 'POST'])
@login_required
def edit_food(food_id):
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    food_to_edit = Food.query.get_or_404(food_id)
    if food_to_edit.school_id not in {None, _school_scope_id()}:
        flash('You do not have permission to edit this food item.', 'danger')
        return redirect(url_for('main.manage_foods'))
    if food_to_edit.school_id is None:
        flash('Default food items are shared across schools and cannot be edited here.', 'warning')
        return redirect(url_for('main.manage_foods'))
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            calories = float(request.form.get('calories'))
            protein = float(request.form.get('protein'))
            carbs = float(request.form.get('carbs'))
            fats = float(request.form.get('fats'))
            if not name:
                raise ValueError('Food name cannot be empty.')
            if min(calories, protein, carbs, fats) < 0:
                raise ValueError('Nutrition values cannot be negative.')
            duplicate = Food.query.filter(Food.id != food_id, Food.name == name).first()
            if duplicate:
                raise ValueError(f'Food item "{name}" already exists.')
            food_to_edit.name = name
            food_to_edit.calories = calories
            food_to_edit.protein = protein
            food_to_edit.carbs = carbs
            food_to_edit.fats = fats
            add_audit_log('update', 'food', entity_id=food_id, details={'school_id': _school_scope_id(), 'name': name})
            if not _commit_session(f"Edit food food_id={food_id} school_id={_school_scope_id()}"):
                flash('Food item could not be updated right now. Please try again.', 'danger')
                return redirect(url_for('main.edit_food', food_id=food_id))

            flash(f'Food item "{food_to_edit.name}" updated!', 'success')
            return redirect(url_for('main.manage_foods'))
        except Exception as e:
            _rollback_session()
            logger.exception("Unexpected error while editing food_id=%s for school_id=%s", food_id, _school_scope_id())
            flash(f'Error updating food item: {e}', 'danger')
            return redirect(url_for('main.edit_food', food_id=food_id))
    return render_template('edit_food.html', food=food_to_edit)
    
@main.route('/delete-food/<int:food_id>', methods=['POST'])
@login_required
def delete_food(food_id):
    if not current_user.can_manage_meals_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))
    if MealPlanItem.query.filter_by(food_id=food_id).first():
        flash('This food item cannot be deleted because it is part of an existing meal plan.', 'danger')
        return redirect(url_for('main.manage_foods'))
    food_to_delete = Food.query.get_or_404(food_id)
    if food_to_delete.school_id not in {_school_scope_id()}:
        flash('Only school-specific food items can be deleted here.', 'danger')
        return redirect(url_for('main.manage_foods'))
    db.session.delete(food_to_delete)
    add_audit_log('delete', 'food', entity_id=food_id, details={'school_id': _school_scope_id(), 'name': food_to_delete.name})
    if not _commit_session(f"Delete food food_id={food_id} school_id={_school_scope_id()}"):
        flash('The food item could not be deleted right now. Please try again.', 'danger')
        return redirect(url_for('main.manage_foods'))

    flash(f'Food item "{food_to_delete.name}" has been deleted.', 'success')
    return redirect(url_for('main.manage_foods'))

# --- AI-POWERED & STUDENT-SPECIFIC ROUTES (from File 1) ---
@main.route("/meals")
@login_required
def meals():
    if not current_user.has_role(User.ROLE_USER):
        flash("This page is for students.", "info")
        return redirect(url_for('main.dashboard'))

    student_detail = _student_detail_or_logout()
    if not student_detail:
        return redirect(url_for('main.login'))
    
    today = date.today()
    try:
        todays_plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
            school_id=student_detail.school_id,
            plan_date=today,
        ).first()
        
        plan_meals = _group_plan_meals(todays_plan) if todays_plan else None
        return render_template('student_meals.html', user=current_user, student=student_detail, today=today, 
                               todays_plan=todays_plan, meals=plan_meals, meal_plan=None, form_data=None)
    except Exception:
        _rollback_session()
        logger.exception("Failed to load student meals page for user_id=%s", current_user.id)
        flash('Today\'s meal plan could not be loaded right now.', 'warning')
        return render_template('student_meals.html', user=current_user, student=student_detail, today=today, todays_plan=None, meals=None, meal_plan=None, form_data=None)

@main.route('/get-meal-plans')
@login_required
def get_meal_plans():
    student = _student_detail_or_logout()
    if not current_user.has_role(User.ROLE_USER) or not student:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        year = int(request.args.get('year'))
        month = int(request.args.get('month'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid month or year'}), 400
    plans = MealPlan.query.filter(
        MealPlan.school_id == student.school_id,
        db.extract('year', MealPlan.plan_date) == year,
        db.extract('month', MealPlan.plan_date) == month
    ).all()
    
    plan_dates = {plan.plan_date.strftime('%Y-%m-%d'): True for plan in plans}
    return jsonify(plan_dates)


@main.route('/get-meal-plan-detail')
@login_required
def get_meal_plan_detail():
    student = _student_detail_or_logout()
    if not current_user.has_role(User.ROLE_USER) or not student:
        return jsonify({'error': 'Unauthorized'}), 403
    date_str = request.args.get('date')
    try:
        plan_date = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid date'}), 400
    plan = MealPlan.query.options(selectinload(MealPlan.items).selectinload(MealPlanItem.food)).filter_by(
        school_id=student.school_id,
        plan_date=plan_date,
    ).first()
    
    if not plan:
        return jsonify(None)

    plan_meals = _group_plan_meals(plan)
    return jsonify(plan_meals)


@main.route('/get-nutrition-data/<iso_date>')
@login_required
def get_nutrition_data(iso_date):
    student = _student_detail_or_logout()
    if not current_user.has_role(User.ROLE_USER) or not student:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        selected_date = date.fromisoformat(iso_date)
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    if selected_date > date.today():
        return jsonify({'error': 'Future dates are not available'}), 400

    nutrition_data = _get_student_nutrition_data(student, selected_date)
    return jsonify(
        {
            'daily_nutrition': nutrition_data['daily_nutrition'],
            'recommended_values': nutrition_data['recommended_values'],
            'nutrition_percentages': nutrition_data['nutrition_percentages'],
            'meal_type_totals': nutrition_data['meal_type_totals'],
            'weekly_nutrition': nutrition_data['weekly_nutrition'],
            'meal_plan': _meal_plan_history_payload(nutrition_data['meal_plan']) if nutrition_data['meal_plan'] else None,
        }
    )

# --- Food Search API ---
@main.route('/search-food')
@limiter.limit('20 per minute')
@login_required
def search_food():
    if not current_user.has_role(User.ROLE_USER):
        return jsonify({'error': 'Unauthorized'}), 403
    query = _normalize_food_query(request.args.get('q', ''))
    if not query:
        return jsonify([])

    local_matches = _search_local_foods(query, school_scope_id=getattr(current_user, 'school_scope_id', None))
    if local_matches:
        return jsonify([_food_to_search_payload(food) for food in local_matches])

    if len(query) < 3:
        return jsonify([])

    try:
        if not check_ai_quota(current_user, 'nutrition_lookup', current_app.config):
            return jsonify([_fallback_nutrition_lookup(query) | {'name': query.title(), 'source': 'quota-fallback'}])
        nutrition_data = copy.deepcopy(_cached_ai_nutrition_lookup(query.casefold()))
        nutrition_data["name"] = query.title()
        _log_ai_usage(current_user, 'nutrition_lookup', 'success', request_units=estimate_request_units(query), details={'source': 'ai-search'})
        return jsonify([nutrition_data])

    except Exception as e:
        logger.warning("Error calling Google AI API or parsing response for search '%s': %s", query, e)
        fallback_nutrition = _fallback_nutrition_lookup(query)
        fallback_nutrition["name"] = query.title()
        _log_ai_usage(current_user, 'nutrition_lookup', 'fallback', request_units=estimate_request_units(query), details={'source': 'search'})
        return jsonify([fallback_nutrition])

@main.route('/health-form', methods=['GET', 'POST'])
@limiter.limit('6 per minute', methods=['POST'])
@login_required
def health_form():
    if not current_user.has_role(User.ROLE_USER):
        flash("This page is for students.", "info")
        return redirect(url_for('main.dashboard'))

    student_detail = _student_detail_or_logout()
    if not student_detail:
        return redirect(url_for('main.login'))

    school = db.session.get(User, student_detail.school_id) if student_detail else None
    school_name = school.school_name if school else "N/A"
    insights = None

    if request.method == 'POST':
        form_data = request.form.to_dict()
        form_data['name'] = student_detail.full_name

        required_fields = ['age', 'sex', 'height', 'weight', 'waist', 'meals_per_day', 'fruit_veg_intake', 'junk_food_intake', 'water_intake', 'sleep_hours', 'physical_activity']
        missing_fields = [field for field in required_fields if not form_data.get(field)]
        if missing_fields:
            flash('Please complete all health form fields before generating insights.', 'danger')
            return render_template('student_health_form.html', insights=None, form_data=form_data, student=student_detail, school_name=school_name)

        prompt = f"""
            You are an experienced child nutritionist practicing in India. Return only JSON for a supportive health summary suitable for a student and their guardians.

            Student Data:
            - Name: {form_data.get('name')}
            - Age: {form_data.get('age')}
            - Sex: {form_data.get('sex')}
            - Height: {form_data.get('height')} cm
            - Weight: {form_data.get('weight')} kg
            - Waist : {form_data.get('waist')} cm
            - Meals per day: {form_data.get('meals_per_day')}
            - Fruits/vegetables intake: {form_data.get('fruit_veg_intake')}
            - Junk food/soft drinks: {form_data.get('junk_food_intake')}
            - Water intake: {form_data.get('water_intake')}
            - Sleep hours: {form_data.get('sleep_hours')}
            - Physical activity: {form_data.get('physical_activity')}

            Output schema:
            {{
              "overall_summary": string,
              "bmi_analysis": {{
                "category": string,
                "comment": string
              }},
              "positive_points": [string],
              "areas_for_improvement": [
                {{
                  "point": string,
                  "recommendation": string
                }}
              ]
            }}
            """

        try:
            if not check_ai_quota(current_user, 'health_insights', current_app.config):
                raise ValueError('Daily health insights quota reached.')
            ai_payload = _call_gemini_json(
                prompt,
                generation_config=_health_generation_config(),
                log_label=f'Health insights for student {student_detail.id}',
            )
            insights = _coerce_health_insights_payload(ai_payload)
            _log_ai_usage(current_user, 'health_insights', 'success', request_units=estimate_request_units(prompt), details={'student_id': student_detail.id})
            
            return render_template('student_health_form.html', insights=insights, form_data=form_data, student=student_detail, school_name=school_name)

        except Exception as e:
            logger.warning("Error generating or parsing health insights for student %s: %s", student_detail.id, e)
            fallback_insights = _fallback_health_insights(form_data)
            _log_ai_usage(current_user, 'health_insights', 'fallback', request_units=estimate_request_units(prompt), details={'student_id': student_detail.id})
            flash("The AI service is unavailable right now, so we generated health insights locally instead.", "warning")
            return render_template('student_health_form.html', insights=fallback_insights, form_data=form_data, student=student_detail, school_name=school_name)

    # For GET request
    return render_template('student_health_form.html', insights=None, form_data=None, student=student_detail, school_name = school_name)
@main.route('/insights')
@login_required
def insights():
    if not current_user.can_view_reports_effective:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('main.dashboard'))

    today = date.today()
    try:
        school_scope_id = _school_scope_id()
        # Get all student details associated with the current school user
        school_students = StudentDetail.query.filter_by(school_id=school_scope_id).all()
        student_ids = [s.id for s in school_students]
        total_students = len(school_students)

        # --- 1. Top Summary Data ---
        unique_students_attended_today_count = db.session.query(func.count(func.distinct(Attendance.student_id))).filter(
            Attendance.student_id.in_(student_ids),
            Attendance.attendance_date == today,
            or_(Attendance.ate_breakfast, Attendance.ate_lunch, Attendance.ate_dinner)
        ).scalar() or 0

        attendance_today_percent = round((unique_students_attended_today_count / total_students) * 100, 1) if total_students > 0 else 0

        seven_days_ago = today - timedelta(days=6)
        total_possible_student_days_7days = total_students * 7 

        unique_student_days_attended_7days = db.session.query(func.count(func.distinct(Attendance.student_id.cast(db.String) + ',' + Attendance.attendance_date.cast(db.String)))).filter(
            Attendance.student_id.in_(student_ids),
            Attendance.attendance_date >= seven_days_ago,
            or_(Attendance.ate_breakfast, Attendance.ate_lunch, Attendance.ate_dinner)
        ).scalar() or 0
        
        avg_weekly_attendance_percent = round((unique_student_days_attended_7days / total_possible_student_days_7days) * 100, 1) if total_possible_student_days_7days > 0 else 0

        current_month, current_year = today.month, today.year
        meals_this_month_count = db.session.query(func.sum(
            (Attendance.ate_breakfast.cast(db.Integer)) +
            (Attendance.ate_lunch.cast(db.Integer)) +
            (Attendance.ate_dinner.cast(db.Integer))
        )).filter(
            Attendance.student_id.in_(student_ids),
            extract('month', Attendance.attendance_date) == current_month,
            extract('year', Attendance.attendance_date) == current_year
        ).scalar() or 0

        # --- 2. Chart Data ---
        attendance_labels = [(today - timedelta(days=i)).strftime('%a %d') for i in range(6, -1, -1)]
        attendance_data = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            attended_on_day_count = db.session.query(func.count(func.distinct(Attendance.student_id))).filter(
                Attendance.student_id.in_(student_ids),
                Attendance.attendance_date == day,
                or_(Attendance.ate_breakfast, Attendance.ate_lunch, Attendance.ate_dinner)
            ).scalar() or 0
            daily_percent = round((attended_on_day_count / total_students) * 100, 1) if total_students > 0 else 0
            attendance_data.append(daily_percent)

        breakfast_served_today = db.session.query(func.count(Attendance.id)).filter(Attendance.student_id.in_(student_ids), Attendance.attendance_date == today, Attendance.ate_breakfast == True).scalar() or 0
        lunch_served_today = db.session.query(func.count(Attendance.id)).filter(Attendance.student_id.in_(student_ids), Attendance.attendance_date == today, Attendance.ate_lunch == True).scalar() or 0
        dinner_served_today = db.session.query(func.count(Attendance.id)).filter(Attendance.student_id.in_(student_ids), Attendance.attendance_date == today, Attendance.ate_dinner == True).scalar() or 0
        meal_distribution_data = [breakfast_served_today, lunch_served_today, dinner_served_today]

        grades_present = db.session.query(func.distinct(StudentDetail.grade)).filter(StudentDetail.school_id == school_scope_id).order_by(StudentDetail.grade).all()
        grades_present = [g[0] for g in grades_present if g[0] is not None]
        nutrition_compliance_labels = [f'Grade {g}' for g in grades_present]
        nutrition_compliance_data = []
        for grade in grades_present:
            students_in_grade_ids = [s.id for s in school_students if s.grade == grade]
            month_ago = today - timedelta(days=30)
            total_possible_meals_per_student_month = 3 * 30
            total_possible_grade_meals_month = len(students_in_grade_ids) * total_possible_meals_per_student_month
            actual_meals_attended_grade = db.session.query(func.sum((Attendance.ate_breakfast.cast(db.Integer)) + (Attendance.ate_lunch.cast(db.Integer)) + (Attendance.ate_dinner.cast(db.Integer)))).filter(
                Attendance.student_id.in_(students_in_grade_ids), Attendance.attendance_date >= month_ago
            ).scalar() or 0
            grade_nutrition_score = round((actual_meals_attended_grade / total_possible_grade_meals_month) * 100, 1) if total_possible_grade_meals_month > 0 else 0
            nutrition_compliance_data.append(grade_nutrition_score)

        # --- 3. Program Impact Data & Class/Grade Insights ---
        health_impact_data = [{'metric': 'BMI Improvement %', 'current_value': 'N/A', 'change': 'N/A'}, {'metric': 'Attendance Rise', 'current_value': f'{avg_weekly_attendance_percent}%', 'change': 'N/A'}]
        class_insights = []
        for grade in grades_present:
            students_in_grade = [s for s in school_students if s.grade == grade]
            students_in_grade_ids = [s.id for s in students_in_grade]
            total_possible_grade_student_days_7days = len(students_in_grade) * 7
            
            unique_grade_student_days_attended_7days = db.session.query(func.count(func.distinct(Attendance.student_id.cast(db.String) + ',' + Attendance.attendance_date.cast(db.String)))).filter(
                Attendance.student_id.in_(students_in_grade_ids),
                Attendance.attendance_date >= seven_days_ago,
                or_(Attendance.ate_breakfast, Attendance.ate_lunch, Attendance.ate_dinner)
            ).scalar() or 0
            
            avg_grade_attendance = round((unique_grade_student_days_attended_7days / total_possible_grade_student_days_7days) * 100, 1) if total_possible_grade_student_days_7days > 0 else 0
            avg_grade_nutrition_score = next((score for i, g in enumerate(grades_present) if g == grade for score in [nutrition_compliance_data[i]]), 0)
            class_insights.append({'name': f'Grade {grade}', 'students': len(students_in_grade), 'avg_attendance': avg_grade_attendance, 'avg_nutrition_score': avg_grade_nutrition_score})

        low_attendance_students = []
        for student in school_students:
            records = Attendance.query.filter_by(student_id=student.id).all()
            total_records = len(records)
            if total_records == 0:
                continue
            present_records = sum(1 for record in records if record.was_present)
            percent = round((present_records / total_records) * 100, 1)
            if percent < 75:
                low_attendance_students.append({'name': student.full_name, 'class': f'Grade {student.grade}-{student.section}', 'attendance_percent': percent})

        return render_template('school_insights.html', 
                               total_students=total_students, attendance_today_percent=attendance_today_percent,
                               avg_weekly_attendance_percent=avg_weekly_attendance_percent, total_meals_this_month=meals_this_month_count,
                               attendance_labels=attendance_labels, attendance_data=attendance_data,
                               meal_distribution_data=meal_distribution_data, nutrition_compliance_labels=nutrition_compliance_labels,
                               nutrition_compliance_data=nutrition_compliance_data, health_impact_data=health_impact_data, class_insights=class_insights,
                               low_attendance_students=low_attendance_students[:10])
    except Exception:
        _rollback_session()
        logger.exception("Failed to load insights dashboard for school_id=%s", current_user.id)
        flash('Insights could not be fully loaded right now.', 'warning')
        return render_template('school_insights.html', **_empty_insights_context(today))

# --- NEW: AI Meal Plan Generator Page ---
@main.route('/meal-generator', methods=['GET', 'POST'])
@limiter.limit('6 per minute', methods=['POST'])
@login_required
def meal_generator():
    if not current_user.has_role(User.ROLE_USER):
        flash("This page is for students.", "info")
        return redirect(url_for('main.dashboard'))

    student = _student_detail_or_logout()
    if not student:
        return redirect(url_for('main.login'))
    
    # This block handles the GET request (initial page load)
    if request.method != 'POST':
        # Corrected: Render student_meals.html for the initial GET request
        return render_template('student_meals.html', student=student, meal_plan=None, form_data=None)

    # This block handles the POST request (when the form is submitted)
    form_data = request.form.to_dict()
    diet_type = form_data.get('diet_type')
    allergies = form_data.get('allergies')
    dislikes = form_data.get('dislikes')
    meal_count = form_data.get('meal_count')

    if not diet_type or not meal_count:
        flash('Please choose a diet type and meal count before generating a plan.', 'danger')
        return render_template('student_meals.html', student=student, meal_plan=None, form_data=form_data)

    latest_metric = student.health_metrics.order_by(HealthMetric.record_date.desc()).first()
    latest_height = latest_metric.height_cm if latest_metric else 'Not provided'
    latest_weight = latest_metric.weight_kg if latest_metric else 'Not provided'
    
    combined_allergies = f"{student.allergies or ''}, {allergies or ''}".strip(', ').strip()
    
    prompt = f"""
    Return only JSON for a personalized Indian meal plan for a student.

    Student Profile:
    - Age: {student.age} years
    - Sex: {student.sex}
    - Latest Height: {latest_height} cm
    - Latest Weight: {latest_weight} kg
    - Activity Level: {student.activity_level or 'Moderately Active'}

    Meal Preferences:
    - Diet Type: {diet_type}
    - Allergies to avoid: {combined_allergies if combined_allergies else 'None'}
    - Disliked foods to avoid: {dislikes if dislikes else 'None'}
    - Number of meals: {meal_count}

    Output schema:
    [
      {{
        "meal_type": string,
        "meal_name": string,
        "calories": number,
        "protein": number,
        "carbs": number,
        "fats": number
      }}
    ]
    """
    try:
        if not check_ai_quota(current_user, 'meal_generator', current_app.config):
            raise ValueError('Daily meal generator quota reached.')
        ai_payload = _call_gemini_json(
            prompt,
            generation_config=_meal_plan_generation_config(),
            log_label=f'Meal plan for student {student.id}',
        )
        meal_plan = _coerce_meal_plan_payload(ai_payload, expected_meals=_meal_count_number(meal_count))
        _log_ai_usage(current_user, 'meal_generator', 'success', request_units=estimate_request_units(prompt), details={'student_id': student.id})
        
        # Corrected: Render student_meals.html after generating the plan
        return render_template('student_meals.html', student=student, meal_plan=meal_plan, form_data=form_data)
        
    except Exception as e:
        logger.warning("AI meal generator error for student %s: %s", student.id, e)
        fallback_plan = _fallback_meal_plan(diet_type, meal_count, combined_allergies, dislikes)
        _log_ai_usage(current_user, 'meal_generator', 'fallback', request_units=estimate_request_units(prompt), details={'student_id': student.id})
        flash("The AI service is unavailable right now, so we generated a meal plan locally instead.", "warning")
        return render_template('student_meals.html', student=student, meal_plan=fallback_plan, form_data=form_data)

@main.route('/awareness')
@login_required
def awareness_page():
    if not current_user.has_role(User.ROLE_USER):
        return redirect(url_for('main.dashboard'))

    # Demo data for the awareness content from official sources
    awareness_content = [
        {
            "title": "WHO's Guide to a Healthy Diet",
            "summary": "Learn the official recommendations from the World Health Organization on fruits, vegetables, sugars, and fats.",
            "link": "https://www.who.int/news-room/fact-sheets/detail/healthy-diet",
            "icon": "🌐",
            "category": "Nutrition"
        },
        {
            "title": "Eat Right India: The Balanced Thali",
            "summary": "Based on FSSAI's guidelines, understand how to create a perfectly balanced Indian meal (Thali) for complete nutrition.",
            "link": "https://eatrightindia.gov.in/eatright-tool-kit.jsp",
            "icon": "🍛",
            "category": "Nutrition"
        },
        {
            "title": "Preventing Iron Deficiency (Anemia)",
            "summary": "From India's National Health Portal: why iron is important and how to get enough from your diet to stay active.",
            "link": "https://www.nhp.gov.in/disease/haematology/anaemia",
            "icon": "🩸",
            "category": "Nutrition"
        },
        {
            "title": "UNICEF on Adolescent Nutrition",
            "summary": "Your body grows a lot during school years. UNICEF explains the special nutritional needs for adolescents.",
            "link": "https://www.unicef.org/india/what-we-do/adolescent-nutrition",
            "icon": "🧑‍🎓",
            "category": "Nutrition"
        },
         {
            "title": "WHO Physical Activity Guidelines",
            "summary": "Moving your body for at least 60 minutes a day is key to building strong bones and a healthy heart.",
            "link": "https://www.who.int/news-room/fact-sheets/detail/physical-activity",
            "icon": "🏃",
            "category": "Exercise"
        },
        {
            "title": "The 5 Moments of Hand Hygiene",
            "summary": "The WHO recommends 5 key moments for hand hygiene to prevent infections and the spread of germs.",
            "link": "https://www.who.int/campaigns/world-hand-hygiene-day",
            "icon": "🙌",
            "category": "Hygiene"
        },
        {
            "title": "Why Sleep is Your Superpower",
            "summary": "Learn how getting 8-10 hours of quality sleep helps you learn better, grow stronger, and stay happy.",
            "link": "https://www.sleepfoundation.org/school-and-sleep/how-much-sleep-do-students-need",
            "icon": "😴",
            "category": "Hygiene"
        },
        {
            "title": "Understanding Food Labels",
            "summary": "FSSAI's guide to reading and understanding the nutrition labels on packaged foods to make healthier choices.",
            "link": "https://www.fssai.gov.in/upload/media/FSSAI_News_Food_Safety_Consumer_Connect_07_02_2019.pdf",
            "icon": "📋",
            "category": "Nutrition"
        }
    ]
    return render_template('student_awareness.html', awareness_content=awareness_content)

@main.route('/recipe-finder')
@login_required
def recipe_finder():
    if not current_user.has_role(User.ROLE_USER):
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.meal_generator'))

# NEW: API Endpoint for generating recipes with AI
@main.route('/get-ai-recipe', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def get_ai_recipe():
    if not current_user.has_role(User.ROLE_USER):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json(silent=True) or {}
    food_name = _normalize_food_query(data.get('food_name'))

    if not food_name:
        return jsonify({'success': False, 'error': 'Food name is required.'}), 400

    try:
        if not check_ai_quota(current_user, 'recipe_lookup', current_app.config):
            raise ValueError('Daily recipe quota reached.')
        recipe_data = copy.deepcopy(_cached_ai_recipe_lookup(food_name.casefold()))
        _log_ai_usage(current_user, 'recipe_lookup', 'success', request_units=estimate_request_units(food_name), details={'food_name': food_name})
        return jsonify({'success': True, 'data': recipe_data})

    except Exception as e:
        logger.warning("Error in get_ai_recipe for '%s': %s", food_name, e)
        fallback_recipe = _fallback_recipe_lookup(food_name)
        _log_ai_usage(current_user, 'recipe_lookup', 'fallback', request_units=estimate_request_units(food_name), details={'food_name': food_name})
        return jsonify({'success': True, 'data': fallback_recipe, 'source': 'fallback'})


@main.route('/get-ai-nutrition', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def get_ai_nutrition():
    if not current_user.can_manage_meals_effective:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    food_name = _normalize_food_query((request.get_json(silent=True) or {}).get('food_name', ''))
    if not food_name:
        return jsonify({'success': False, 'error': 'Food name is required'}), 400

    local_food = _find_exact_food(food_name, _school_scope_id())
    if local_food:
        return jsonify({'success': True, 'data': _food_to_nutrition_payload(local_food), 'source': 'local'})

    try:
        if not check_ai_quota(current_user, 'nutrition_lookup', current_app.config):
            raise ValueError('Daily nutrition lookup quota reached.')
        nutrition_data = copy.deepcopy(_cached_ai_nutrition_lookup(food_name.casefold()))
        _log_ai_usage(current_user, 'nutrition_lookup', 'success', request_units=estimate_request_units(food_name), details={'food_name': food_name})
        return jsonify({'success': True, 'data': nutrition_data, 'source': 'ai'})

    except Exception as e:
        logger.warning("Error in get_ai_nutrition for '%s': %s", food_name, e)
        fallback_nutrition = _fallback_nutrition_lookup(food_name)
        _log_ai_usage(current_user, 'nutrition_lookup', 'fallback', request_units=estimate_request_units(food_name), details={'food_name': food_name})
        return jsonify({'success': True, 'data': fallback_nutrition, 'source': 'fallback'})
