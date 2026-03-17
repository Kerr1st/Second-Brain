"""Prompt templates for the Dream Cycle four-agent pipeline.

Re-exports the main prompt builder functions for Explorer, Thinker, and Panel.
"""

from src.prompts.explorer import get_explorer_prompt
from src.prompts.thinker import get_thinker_prompt
from src.prompts.panel import get_evaluator_prompt

__all__ = ["get_explorer_prompt", "get_thinker_prompt", "get_evaluator_prompt"]
