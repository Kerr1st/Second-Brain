"""Dream Cycle entry point, loaded lazily to keep repository imports acyclic."""
__all__ = ["DreamCycleOrchestrator"]

def __getattr__(name):
    if name == "DreamCycleOrchestrator":
        from src.dream_cycle.orchestrator import DreamCycleOrchestrator
        return DreamCycleOrchestrator
    raise AttributeError(name)
