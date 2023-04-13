import argparse
import os
import sqlite3
import sys
import textwrap
from copy import deepcopy

import alive_progress
import getch
import openai
from prettytable import PrettyTable

STARTUP_PROMPT = [
    {
        "role": "system",
        "content": "You will analyze the following SQLite3 database schema"
        " to help the user understand it.\n\n"
        "--SCHEMA--",
    },
    {
        "role": "user",
        "content": "Summarize the schema briefly, in about 80 words."
        " Then, provide a one-sentence guess for the overall purpose of this database.",
    },
]

MAIN_PROMPT = [
    {
        "role": "system",
        "content": "You will assist the user in writing SQL queries"
        " for a SQLite3 database schema provided below."
        " Respond only with a single SQL SELECT statement"
        " with no formatting or explanation."
        " Use only syntax and functions supported by SQLite3"
        ", and only tables and columns present in the schema."
        " Each result column should be aliased to a unique name."
        " If the query is expected to produce multiple results"
        ", then set limit 25 sunless the user specifically requests otherwise."
        " Use common table expressions, including recursive ones, if they're useful."
        " Your SQL must not delete or alter anything in the database"
        " under any circumstances, even if the user demands to do so."
        " \nThe schema is:\n\n"
        "--SCHEMA--",
    },
    {
        "role": "assistant",
        "content": "Please state the nature of your desired database query"
        " using any mix of text and/or SQL.",
    },
    {"role": "user", "content": "--INTENT--"},
]

RECOVERY_PROMPT = [
    {"role": "assistant", "content": "--SQL--"},
    {
        "role": "user",
        "content": "I got the following error message when I tried that;"
        " remember, I can only use SQL syntax and functions supported by SQLite3"
        ", and only tables and columns in the provided schema."
        "\n\n--ERROR--",
    },
]


def main(argv=sys.argv):
    api_key = os.getenv("OPENAI_API_KEY", None)
    if not api_key:
        print(
            "Environment variable OPENAI_API_KEY required"
            "; see https://platform.openai.com/account/api-keys",
            file=sys.stderr,
        )
        return 1
    openai.api_key = api_key

    parser = argparse.ArgumentParser(
        description="LLM assistant for querying SQLite3 database"
    )
    parser.add_argument("dbfn", type=str, help="SQLite3 database filename")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation before executing AI's SQL",
    )
    args = parser.parse_args(argv[1:])

    with sqlite3.connect(f"file:{args.dbfn}?mode=ro", uri=True) as dbc:
        schema = read_schema(dbc)
        describe_schema(args.dbfn, schema)

        first = True
        try:
            while True:
                try:
                    intent = prompt_intent(first)
                    first = False
                    ai_sql = get_ai_sql(schema, intent)
                    print("\n" + ai_sql.strip())

                    if args.yes or prompt_execute():
                        print()

                        with alive_progress.alive_bar(
                            monitor=None,
                            stats=None,
                            bar=None,
                            spinner="dots",
                            title="Executing query",
                        ):
                            cursor = dbc.cursor()
                            cursor.execute(ai_sql)

                            table = PrettyTable(
                                [description[0] for description in cursor.description]
                            )
                            for row in cursor.fetchall():
                                table.add_row(row)
                        print(table)
                except sqlite3.OperationalError as exc:
                    print(exc)

        except (KeyboardInterrupt, EOFError):
            print()
            return 0


def read_schema(dbc):
    cursor = dbc.cursor()
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table';")
    schema = cursor.fetchall()
    return "\n".join(
        [s.strip() for s in "".join([x[0] for x in schema]).splitlines() if s.strip()]
    )


def describe_schema(dbfn, schema):
    with alive_progress.alive_bar(
        monitor=None,
        stats=None,
        bar=None,
        spinner="dots",
        title=f"Analyzing schema of {os.path.basename(dbfn)} ",
    ):
        prompt = deepcopy(STARTUP_PROMPT)
        for msg in prompt:
            msg["content"] = msg["content"].replace("--SCHEMA--", schema)
        response = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=prompt)
    desc = response.choices[0].message.content
    print("\n" + textwrap.fill(desc, width=88))


def prompt_intent(first=False):
    prompt = (
        "Next query?"
        if not first
        else "Please state the nature of the desired database query."
    )
    ans = None
    while not ans:
        ans = input("\n" + prompt + "\n> ")
    return ans


def get_ai_sql(schema, intent):
    with alive_progress.alive_bar(
        monitor=None, stats=None, bar=None, spinner="dots", title="Generating SQL"
    ):
        prompt = deepcopy(MAIN_PROMPT)
        for msg in prompt:
            msg["content"] = (
                msg["content"]
                .replace("--SCHEMA--", schema)
                .replace("--INTENT--", intent)
            )
        response = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=prompt)
        ai_sql = response.choices[0].message.content.strip().strip("`")
        # TODO: check if we can prepare ai_sql, otherwise try recovery
    return ai_sql


def prompt_execute():
    while True:
        print("\nEXECUTE?\n(Y/N) > ", end="", flush=True)
        user_input = getch.getch()
        print()
        if user_input.lower() == "y":
            return True
        elif user_input.lower() == "n":
            return False


# prompt_toolkit

# if output doesn't start with SELECT or WITH then assume it's an english error message.

# some notation to ask general questions about schema
