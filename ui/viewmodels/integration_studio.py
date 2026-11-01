# ui/viewmodels/integration_studio.py
from typing import List, Dict, Any
from core.integration.uber_adapter import GigPlatformRegistry, WorkflowMapper

class IntegrationStudioViewModel:
    def __init__(self):
        self.selected_platform: str = "Uber Driver API"
        self.source_fields: List[Dict[str, str]] = GigPlatformRegistry.fetch_source_fields(self.selected_platform)
        self.active_mappings: List[Dict[str, str]] = []

    def change_platform(self, platform: str):
        self.selected_platform = platform
        self.source_fields = GigPlatformRegistry.fetch_source_fields(platform)
        self.active_mappings.clear()

    def add_mapping(self, source_field: str, target_column: str):
        self.active_mappings.append({"source_field": source_field, "target_column": target_column})

    def generate_code(self) -> Dict[str, Any]:
        return WorkflowMapper.build_query_payload(self.active_mappings)
