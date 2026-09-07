from pagebound.tables import render_table


def cell(text, row, col, header=False):
    return {
        "text": text,
        "start_row_offset_idx": row,
        "start_col_offset_idx": col,
        "column_header": header,
    }


def test_a_header_row_gets_a_separator():
    data = {
        "num_rows": 2, "num_cols": 2,
        "table_cells": [
            cell("Term", 0, 0, header=True), cell("Meaning", 0, 1, header=True),
            cell("AI", 1, 0), cell("Artificial intelligence", 1, 1),
        ],
    }
    assert render_table(data) == (
        "| Term | Meaning |\n"
        "| --- | --- |\n"
        "| AI | Artificial intelligence |"
    )


def test_missing_cells_render_as_empty():
    data = {
        "num_rows": 2, "num_cols": 2,
        "table_cells": [cell("A", 0, 0, header=True), cell("B", 1, 1)],
    }
    assert render_table(data) == "| A |  |\n| --- | --- |\n|  | B |"


def test_a_table_without_a_header_row_still_gets_a_separator():
    data = {
        "num_rows": 1, "num_cols": 2,
        "table_cells": [cell("A", 0, 0), cell("B", 0, 1)],
    }
    assert render_table(data) == "| A | B |\n| --- | --- |"


def test_pipes_in_a_cell_are_escaped():
    data = {
        "num_rows": 1, "num_cols": 1,
        "table_cells": [cell("a | b", 0, 0, header=True)],
    }
    assert render_table(data) == "| a \\| b |\n| --- |"


def test_newlines_in_a_cell_become_spaces():
    data = {
        "num_rows": 1, "num_cols": 1,
        "table_cells": [cell("two\nlines", 0, 0, header=True)],
    }
    assert render_table(data) == "| two lines |\n| --- |"


def test_an_empty_table_renders_as_nothing():
    assert render_table({"num_rows": 0, "num_cols": 0, "table_cells": []}) == ""
