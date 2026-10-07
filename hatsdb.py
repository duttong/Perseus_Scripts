#!/usr/bin/env python3
"""
A minimal CLI for interacting with the HATS database using HATS_DB_Functions.
"""

import typer
import pandas as pd
from typing_extensions import Annotated

# This script assumes it is run from a context where logos_instruments is importable.
# The HATS_DB_Functions class itself handles the path for its own db_utils dependency.
from logos_instruments import HATS_DB_Functions, LOGOS_Instruments

app = typer.Typer(
    help="A minimal CLI for interacting with the HATS database.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Use a state dictionary to hold the global instrument ID
state = {"inst_id": "fe3"}

@app.callback()
def main_callback(
    inst: Annotated[
        str,
        typer.Option(
            "--inst",
            "-i",
            help="Instrument ID to use for context-specific queries.",
            # Add autocompletion for known instruments
            autocompletion=lambda: list(LOGOS_Instruments.INSTRUMENTS.keys()),
        ),
    ] = "fe3"
):
    """
    HATS DB CLI. Select an instrument globally with --inst.
    """
    state["inst_id"] = inst


@app.command()
def raw(query: Annotated[str, typer.Argument(help="The raw SQL query to execute.")]):
    """
    Execute a raw SQL query and print the results as a table.
    """
    # For raw queries, inst_id is not strictly needed but the class requires it.
    db_conn = HATS_DB_Functions(inst_id=state["inst_id"])
    try:
        result = db_conn.doquery(query)
        if result:
            df = pd.DataFrame(result)
            print(df.to_string())
        else:
            print("Query executed. No results returned (or it was an UPDATE/INSERT/DELETE).")
    except Exception as e:
        typer.secho(f"An error occurred: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

@app.command()
def sites():
    """List GML sites from the database."""
    db_conn = HATS_DB_Functions(inst_id=state["inst_id"])
    sites_dict = db_conn.gml_sites()
    df = pd.DataFrame(list(sites_dict.items()), columns=['code', 'num'])
    print(df.to_string(index=False))

@app.command()
def analytes():
    """List analytes for the selected instrument."""
    db_conn = HATS_DB_Functions(inst_id=state["inst_id"])
    analytes_dict = db_conn.query_analytes()
    df = pd.DataFrame(list(analytes_dict.items()), columns=['display_name', 'param_num'])
    print(df.to_string(index=False))

@app.command()
def run_types():
    """List all available run types and their numbers."""
    db_conn = HATS_DB_Functions(inst_id=state["inst_id"])
    run_types_dict = db_conn.run_type_num()
    df = pd.DataFrame(list(run_types_dict.items()), columns=['name', 'num'])
    print(df.to_string(index=False))


if __name__ == "__main__":
    app()