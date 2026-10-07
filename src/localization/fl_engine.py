"""Compatibility import for the serial repair controller."""
from src.localization.repair_engine import RepairOrchestrator

FaultLocalizationEngine = RepairOrchestrator

__all__ = ["FaultLocalizationEngine", "RepairOrchestrator"]
