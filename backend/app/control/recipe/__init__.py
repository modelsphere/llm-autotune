"""Recipes: engine configurations as the chart and the model catalog read them.

model.py     Recipe: chart + values (the variant's catalog layer)
args.py      extraArgs read and edited as flags, untouched text kept
address.py   flat config keys as addresses into a recipe
convert.py   apply(base, config) / diff(base, recipe), inverses
shape.py     pods and cards a recipe claims
compose.py   recipe + site + run -> LLMService
check.py     what would stop it launching or publishing
legacy.py    today's campaign fields -> a recipe
"""

from app.control.recipe.convert import apply, canonical, diff, full_config
from app.control.recipe.model import ChartRef, Recipe
from app.control.recipe.shape import Shape, shape

__all__ = ["ChartRef", "Recipe", "Shape", "apply", "canonical", "diff", "full_config", "shape"]
