#!/usr/bin/env python3
"""
A simple library for connecting to the HATS database.
"""

import sys
import pandas as pd

class HATSdb:
    """A simple interface to the HATS database, wrapping db_conn.HATS_ng()."""

    def __init__(self):
        # Add the database utility path and establish a connection,
        # mirroring the approach in logos_instruments.py.
        sys.path.append('/ccg/src/db/')
        try:
            import db_utils.db_conn as db_conn
            self.db = db_conn.HATS_ng()
        except ImportError as e:
            raise ImportError(
                "Could not import db_utils.db_conn. "
                "Ensure /ccg/src/db is in your Python path."
            ) from e

    def doquery(self, query: str, params=None):
        """
        Executes a raw SQL query and returns the result.

        Args:
            query (str): The SQL query to execute.
            params (list, optional): Parameters to pass to the query. Defaults to None.

        Returns:
            The result from the database query.
        """
        return self.db.doquery(query, params)

    def to_df(self, query: str, params=None) -> pd.DataFrame:
        """
        Executes a SQL query and returns the results as a pandas DataFrame.
        """
        result = self.doquery(query, params)
        if result:
            return pd.DataFrame(result)
        return pd.DataFrame()

if __name__ == '__main__':
    # Example usage:
    print("Connecting to HATS database...")
    db = HATSdb()
    print("Connection successful. Fetching GML sites as an example.")
    sites_df = db.to_df("SELECT code, name FROM gmd.site ORDER BY code LIMIT 5;")
    print("\nFirst 5 GML flask sites:")
    print(sites_df.to_string(index=False))