# core/integration/uber_adapter.py
from typing import Dict, List, Any

class GigPlatformRegistry:
    """Registry providing dynamic source fields for gig work platforms like Uber, Lyft, etc."""
    
    @staticmethod
    def get_available_platforms() -> List[str]:
        return ["Uber Driver API", "Lyft Driver API", "DoorDash Dispatch", "Instacart Shopper"]

    @staticmethod
    def fetch_source_fields(platform_name: str) -> List[Dict[str, str]]:
        """Dynamically fetches/loads available source data fields for the selected platform."""
        if "Uber" in platform_name:
            return [
                {"field": "trip_id", "type": "string", "description": "Unique Trip Identifier"},
                {"field": "start_latitude", "type": "float", "description": "Pickup Latitude Coordinates"},
                {"field": "start_longitude", "type": "float", "description": "Pickup Longitude Coordinates"},
                {"field": "end_latitude", "type": "float", "description": "Dropoff Latitude Coordinates"},
                {"field": "end_longitude", "type": "float", "description": "Dropoff Longitude Coordinates"},
                {"field": "fare_amount", "type": "integer", "description": "Integer-math stored fare (cents)"},
                {"field": "surge_multiplier", "type": "float", "description": "Active Surge Multiplier"},
                {"field": "driver_online_duration", "type": "integer", "description": "Seconds online in session"}
            ]
        # Generic fallback or additional platform schemas
        return [
            {"field": "event_id", "type": "string", "description": "Generic Event ID"},
            {"field": "timestamp", "type": "integer", "description": "Epoch timestamp"}
        ]

class WorkflowMapper:
    """Handles visual field-to-query mappings for the Taleo-style canvas."""
    
    @staticmethod
    def build_query_payload(mappings: List[Dict[str, str]]) -> Dict[str, Any]:
        query_code = {}
        for map_item in mappings:
            source = map_item.get("source_field")
            target = map_item.get("target_column")
            if source and target:
                query_code[target] = f"{{source.{source}}}"
        return {
            "version": "1.0",
            "engine": "iflow-secure-pipeline",
            "mappings": query_code
        }
