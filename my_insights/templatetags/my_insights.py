from django import template

from ..dashboard import is_strong, tile_color_class
from ..loading import display_value

register = template.Library()


@register.filter
def cell(row: dict, key: str) -> str:
    """A data cell as the user should read it, e.g. {{ row|cell:column.name }}."""
    return display_value(row.get(key))


@register.filter
def shown(row: dict, key: str) -> str:
    """A query result cell, already formatted (e.g. "₹ 1,23,456"): {{ row|shown:column.name }}."""
    return row.get("t" + key, "")


@register.filter
def tile_color(tile) -> str:
    """A KPI / note card's colour classes, e.g. "bg-success/10 border-success/40"."""
    return tile_color_class(tile)


@register.filter
def tile_strong(tile) -> bool:
    """True on a full-colour card: its number and links use the card's text colour."""
    return is_strong(tile)
