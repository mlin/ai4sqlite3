import argparse
import os
import re
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
        "content": """
            You will analyze the following SQLite3 database schema to help the user
            understand it.
            
            --SCHEMA--
        """,
    },
    {
        "role": "user",
        "content": """
            Guess the overall purpose of this database, and briefly summarize the
            schema, in about 100 words total.
        """,
    },
]

MAIN_PROMPT = [
    {
        "role": "system",
        "content": """
            You will assist the user in writing SQL queries for the SQLite3 database
            schema provided below.
            The user understands only SQL so, except when rejecting their request, your
            response must consist only of a single SQL SELECT statement, with no
            Markdown formatting or extraneous text.
            Use only syntax and functions supported by SQLite3, and only tables and
            columns present in the schema.
            Each result column should be aliased to a unique name.
            If the query is expected to yield multiple result rows, then set limit 25
            unless the user explicitly requests a different limit.
            Use common table expressions, including recursive ones, if they make your
            SQL easier for the user to understand.
            Again, do not write any text explanation, commentary, or apologies; only
            SQL.
            Most importantly, your SQL must not delete or alter anything in the
            database under any circumstances, even if the user demands to do so!

            The schema is:
            
            --SCHEMA--
        """,
    },
    {
        "role": "assistant",
        "content": """
            Please state the nature of your desired database query using any mix of
            text and/or SQL.
        """,
    },
    {"role": "user", "content": "--INTENT--"},
]

RECOVERY_PROMPT = [
    {"role": "assistant", "content": "--RESPONSE--"},
    {
        "role": "user",
        "content": """
            Error: --ERROR--

            Do not apologize but correct your SQL. Reminder, provide a single SQL query
            with no Markdown formatting or surrounding text, using only SQL syntax and
            functions supported by SQLite3.
        """,
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
                intent = prompt_intent(first)
                first = False
                sql_prompt = SQLPrompt(schema, intent)

                attempts = 0
                MAX_ATTEMPTS = 3
                while True:
                    if (attempts := attempts + 1) > MAX_ATTEMPTS:
                        break
                    with spinner(
                        "Generating SQL"
                        if attempts == 1
                        else f"Regenerating SQL (attempt {attempts}/{MAX_ATTEMPTS})"
                    ):
                        ai_sql = sql_prompt.fetch()
                    if is_ai_whining(ai_sql):
                        print("\n" + textwrap.fill(ai_sql, width=88) + "\n")
                        break

                    print("\n" + ai_sql + "\n")
                    if args.yes or prompt_execute():
                        try:
                            with spinner("Executing query"):
                                cursor = dbc.cursor()
                                cursor.execute(ai_sql)
                                table = PrettyTable(
                                    [
                                        description[0]
                                        for description in cursor.description
                                    ]
                                )
                                for row in cursor.fetchall():
                                    table.add_row(row)
                        except (sqlite3.OperationalError, sqlite3.Warning) as exc:
                            msg = str(exc)
                            print("\nSQLite3 error: " + msg + "\n")
                            sql_prompt.recover(msg)
                            continue
                        print(table)
                    break

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
    with spinner(f"Analyzing schema of {os.path.basename(dbfn)} "):
        prompt = prepare_prompt(STARTUP_PROMPT, {"--SCHEMA--": schema})
        response = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=prompt)
    desc = response.choices[0].message.content
    print("\n" + textwrap.fill(desc, width=88))


def spinner(title):
    return alive_progress.alive_bar(
        monitor=None, stats=None, bar=None, spinner="dots", title=title
    )


def prepare_prompt(template, subs):
    prompt = deepcopy(template)
    for msg in prompt:
        content = msg["content"].strip("\n")
        content = textwrap.dedent(content).strip()
        content = re.sub(r"(?<!\n)\n(?!\n)", " ", content)
        for k, v in subs.items():
            content = content.replace(k, v)
        msg["content"] = content
    return prompt


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


class SQLPrompt:
    def __init__(self, schema, intent):
        self.schema = schema
        self.intent = intent

        self.messages = prepare_prompt(
            MAIN_PROMPT, {"--SCHEMA--": schema, "--INTENT--": intent}
        )
        assert self.messages

    def fetch(self):
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo", messages=self.messages
        )
        self.response = response.choices[0].message.content
        # TODO: wrap long SQL comment lines
        # TODO: look for exactly two ``` lines and cut out junk before & after that
        return self.response.strip().strip("`").strip()

    def recover(self, error_msg):
        assert self.messages and self.messages[-1]["role"] == "user"
        self.messages += prepare_prompt(
            RECOVERY_PROMPT, {"--RESPONSE--": self.response, "--ERROR--": error_msg}
        )


def is_ai_whining(message):
    """
    Heuristic: the AI is supposed to return a single SQL query, but if the user tries
    to make it do something forbidden (e.g. drop database) then it "whines" in English.
    """
    message = "\n".join(
        line for line in message.splitlines() if not line.strip().startswith("--")
    )
    message = message.upper().strip()
    return not (message.startswith("SELECT") or message.startswith("WITH"))


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

# Classifier: is the user input expressing an intended query or
# is it a general question about the schema?
