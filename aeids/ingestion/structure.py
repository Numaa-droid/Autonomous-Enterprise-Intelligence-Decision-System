"""TableLens recognition output (``output/<name>/structure/<name>_structure.json``) as typed models.

The file is untrusted input (CLAUDE.md rule 7): it is validated here and its text is only ever
stored and searched as data. Unknown fields are ignored so TableLens can add fields freely.
Coordinates are PDF points, ``[x0, y0, x1, y1]``, origin top-left of the unrotated page.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from aeids.documents import InvalidUpload


class InvalidStructure(InvalidUpload):
    code = "invalid_structure"
    http_status = 400


def _clean_text(value: Any) -> Any:
    # Postgres text cannot hold NUL; other control characters are harmless but useless.
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _bbox(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("bbox must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (float(v) for v in value)
    if x1 < x0 or y1 < y0:
        raise ValueError("bbox must have x0 <= x1 and y0 <= y1")
    return [round(v, 2) for v in (x0, y0, x1, y1)]


Text = Annotated[str | None, BeforeValidator(_clean_text)]
BBox = Annotated[list[float] | None, BeforeValidator(_bbox)]
Page = Annotated[int, Field(ge=1, le=100_000)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Cell(_Model):
    @model_validator(mode="before")
    @classmethod
    def _drop_inverted_box(cls, data: Any) -> Any:
        """A cell box with x1 < x0 or y1 < y0 (TableLens can emit one for a degenerate row) is
        dropped, not guessed: the cell keeps its text, its box becomes unknown."""
        if isinstance(data, dict) and (b := data.get("bbox_pdf_pt")) is not None:
            try:
                _bbox(b)
            except (ValueError, TypeError):
                data = {**data, "bbox_pdf_pt": None, "bbox_dropped": True}
        return data

    bbox_dropped: bool = False
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    column_header: bool = False
    row_header: bool = False
    row_section: bool = False
    text: Text = None
    bbox_pdf_pt: BBox = None
    # joined tables only: the part (page, table_id) the cell came from
    page: Page | None = None
    table_id: int | None = None


class _Grid(_Model):
    num_rows: int = Field(ge=0, le=10_000)
    num_cols: int = Field(ge=0, le=500)
    title: Text = None
    subtitle: Text = None
    cells: list[Cell] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cells_inside_grid(self) -> _Grid:
        for c in self.cells:
            if c.row >= self.num_rows or c.col >= self.num_cols:
                raise ValueError(
                    f"cell ({c.row}, {c.col}) outside a {self.num_rows}x{self.num_cols} grid"
                )
        return self


class Table(_Grid):
    page: Page
    table_id: int
    bbox_pdf_pt: BBox
    order: int | None = None
    orientation: int = 0
    engine: Text = None
    route: Text = None
    complete: bool = True
    agreement: float | None = None


class TablePart(_Model):
    page: Page
    table_id: int


class JoinedTable(_Grid):
    """A table printed over several pages, joined by TableLens; cells carry their part."""

    parts: list[TablePart] = Field(min_length=2)


class ChartTable(_Model):
    num_rows: int = Field(ge=0)
    num_cols: int = Field(ge=0)
    header: list[Annotated[str, BeforeValidator(_clean_text)]] = Field(default_factory=list)
    rows: list[list[Annotated[str, BeforeValidator(_clean_text)]]] = Field(default_factory=list)


class ChartCheck(_Model):
    status: str | None = None
    not_in_pdf: list[str] = Field(default_factory=list)


class Region(_Model):
    page: Page
    id: int
    label: str
    bbox_pdf_pt: BBox
    order: int | None = None
    text: Text = None
    complete: bool = True
    read_by: Text = None
    chart_table: ChartTable | None = None
    check: ChartCheck | None = None


class PageOrder(_Model):
    page: Page
    items: list[tuple[str, int]] = Field(default_factory=list)


class Structure(_Model):
    file: Text = None
    model: Text = None
    pages: list[PageOrder] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    joined_tables: list[JoinedTable] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)

    def max_page(self) -> int:
        pages = [t.page for t in self.tables] + [r.page for r in self.regions]
        pages += [p.page for p in self.pages]
        return max(pages, default=0)


def parse_structure(raw: bytes | str) -> Structure:
    try:
        structure = Structure.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise InvalidStructure(
            f"not a valid TableLens _structure.json ({exc.error_count()} error(s); first at "
            f"{where or 'top level'}: {first.get('msg')})"
        ) from None
    if not structure.tables and not structure.regions:
        raise InvalidStructure("the _structure.json has no tables and no regions")
    return structure
