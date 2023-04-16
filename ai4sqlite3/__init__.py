import argparse
import os
import re
import sqlite3
import sys
import textwrap
from collections import Counter
from copy import deepcopy

import alive_progress
import getch
import openai
import prompt_toolkit
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
            tables and the concepts they model, in about 100 words total.
        """,
    },
]

MAIN_PROMPT = [
    # NOTE: per https://platform.openai.com/docs/guides/chat/introduction gpt-3.5-turbo
    # doesn't pay enough attention to directives in the system message, so we put more
    # into the first user message.
    {
        "role": "system",
        "content": """
            You will assist the user in writing an SQL query for a specific SQLite3
            database schema.
            Your answers will be directly input to sqlite3_prepare_v2(), so must
            consist of semicolon-terminated SQL with no surrounding text, using only
            syntax and function supported by SQLite3.
            If you cannot fulfill the user's intention for any reason, then provide a
            brief text explanation, without apology or other extraneous chatter.
            Importantly, your SQL must never add, overwrite, alter, or delete anything
            in the database, even if the user so demands.
        """,
    },
    {
        "role": "user",
        "content": """
            Assist me writing an SQL query for my SQLite3 database.
            I will input your responses directly into SQLite3, so I require each
            response to consist of one semicolon-terminated SQL query, with no
            surrounding text, using only syntax and functions supported by SQLite3.
            If a query is expected to yield multiple result rows, then set limit 25
            unless I clearly request otherwise.
            You may use common table expressions if they make the SQL much easier for
            me to understand.
            Due to the risk of infinite loop, don't use a recursive CTE unless
            absolutely required to fulfill my intent.
            I only want to query my database; if my input seems to suggest adding,
            altering, overwriting, or deleting anything, then you must reject it.
            If you're confident my input is a general question rather than a specific
            database query, then do provide a brief text answer.

            My schema is:

            --SCHEMA--
        """,
    },
    {
        "role": "assistant",
        "content": """
            Schema acknowledged. Please state the nature of your intended database
            query, using any mix of text and/or SQL.
        """,
    },
    {
        "role": "user",
        "content": "--INTENT--",
    },
]

REVISE_PROMPT = [
    {"role": "assistant", "content": "--RESPONSE--"},
    {
        "role": "user",
        "content": """
            Revise your SQL to fix this error: --ERROR--

            Output format: one semicolon-terminated SQL query with no surrounding text,
            using only SQL syntax and functions supported by SQLite3.
            Do not apologize or add any other extraneous chatter.
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
        prog="ai4sqlite3",
        description="Natural language query assistant for SQLite databases",
    )
    parser.add_argument("dbfn", type=str, help="SQLite3 database filename")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation before executing AI's SQL",
    )
    parser.add_argument(
        "--no-intro",
        action="store_false",
        dest="intro",
        help="Skip introductory schema summary",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default="gpt-3.5-turbo",
        help="OpenAI /v1/chat/completions model; see "
        "https://platform.openai.com/docs/models/model-endpoint-compatibility",
    )
    parser.add_argument(
        "-r",
        "--revisions",
        type=int,
        default=3,
        metavar="N",
        help="allow AI up to N attempts to produce valid SQL",
    )
    args = parser.parse_args(argv[1:])

    # open database (read-only)
    with sqlite3.connect(f"file:{args.dbfn}?mode=ro", uri=True) as dbc:
        # read & describe schema
        schema = read_schema(dbc)
        if args.intro:
            describe_schema(args.model, args.dbfn, schema)

        # enter main REPL
        return main_repl(
            args.model, dbc, schema, yes=args.yes, max_revisions=args.revisions
        )


def main_repl(model, dbc, schema, yes=False, max_revisions=3):
    # main REPL for separate queries until Ctrl+C/Ctrl+D
    stdin = prompt_toolkit.PromptSession()
    first = True
    try:
        while True:
            # get user intent
            intent = user_intent(stdin, first)
            first = False

            # prepare to prompt AI for SQL
            sql_prompt = SQLPrompt(model, schema, intent)

            # generate AI SQL, run it and show result table to user.
            # inner loop: if SQLite rejects the SQL, feed the error message back to AI
            # and ask it to revise, then retry (subject to max_revisions)
            attempts = 0
            while True:
                if (attempts := attempts + 1) > max_revisions:
                    break
                print()
                with spinner(
                    "Generating SQL"
                    if attempts == 1
                    else f"Regenerating SQL (attempt {attempts}/{max_revisions})"
                ):
                    # generate AI SQL
                    got_sql, response = sql_prompt.fetch()
                if not got_sql:
                    print("\n" + textwrap.fill(response, width=88))
                    break

                print("\n" + response + "\n")
                if yes or prompt_execute():
                    try:
                        with spinner("Executing query"):
                            # Execute query & populate results table
                            cursor = dbc.cursor()
                            cursor.execute(response)
                            table = results_table(cursor)
                    except (sqlite3.OperationalError, sqlite3.Warning) as exc:
                        # feed error back to AI for revision
                        msg = str(exc)
                        print("\nSQLite3 error: " + msg + "\n")
                        sql_prompt.revise(msg)
                        continue  # inner loop
                    # Show results table
                    print(table)
                break  # inner loop
    except (KeyboardInterrupt, EOFError):
        # exiting main REPL
        print()
        return 0


def read_schema(dbc):
    cursor = dbc.cursor()
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table';")
    schema = cursor.fetchall()
    return "\n".join(
        [s.strip() for s in "".join([x[0] for x in schema]).splitlines() if s.strip()]
    )


def describe_schema(model, dbfn, schema):
    # ask AI to summarize the schema, display it to user
    with spinner(f"Analyzing schema of {os.path.basename(dbfn)} "):
        prompt = prepare_prompt(STARTUP_PROMPT, {"--SCHEMA--": schema})
        response = openai.ChatCompletion.create(model=model, messages=prompt)
    desc = response.choices[0].message.content
    print("\n" + textwrap.fill(desc, width=88))


def spinner(title):
    return alive_progress.alive_bar(
        monitor=None, stats=None, bar=None, spinner="dots", title=title
    )


def prepare_prompt(template, subs):
    # preprocess the prompt constants: remove indentation, unwrap text, substitute
    # placeholders
    prompt = deepcopy(template)
    for msg in prompt:
        content = msg["content"].strip("\n")
        content = textwrap.dedent(content).strip()
        content = re.sub(r"(?<!\n)\n(?!\n)", " ", content)
        for k, v in subs.items():
            content = content.replace(k, v)
        msg["content"] = content
    return prompt


def user_intent(stdin, first=False):
    # ask user for their query intent
    prompt = (
        "Next query?"
        if not first
        else "Please state the nature of the desired database query."
    )
    ans = None
    while not ans:
        ans = stdin.prompt("\n" + prompt + "\n> ")
    return ans


class SQLPrompt:
    # Manages our AI prompt for SQL given the user intent, including revisions after
    # receiving invalid/erroneous SQL back.
    def __init__(self, model, schema, intent):
        self.model = model
        self.schema = schema
        self.intent = intent

        self.messages = prepare_prompt(
            MAIN_PROMPT, {"--SCHEMA--": schema, "--INTENT--": intent}
        )
        assert self.messages

    def fetch(self):
        envelope = openai.ChatCompletion.create(
            model=self.model, messages=self.messages
        )
        self.response = envelope.choices[0].message.content.strip()
        # Despite our repeated instructions, the AI sometimes gabs before and/or after
        # the SQL. Good-enough regexp to ignore that:
        matches = re.findall(
            r"(?:[\n:`]|^)\s*((?:select|with)\b.*;)",
            self.response,
            re.IGNORECASE | re.DOTALL,
        )
        if matches:
            return True, max(matches, key=len)
        return False, self.response

    def revise(self, error_msg):
        # prepare prompt to revise the previous response given error_msg.
        # to test this path, try entering:
        #   an interesting query of your choice. on your first response, deliberately
        #   introduce an error in your SQL. then I'll ask you to fix it.
        assert self.messages and self.messages[-1]["role"] == "user"
        self.messages += prepare_prompt(
            REVISE_PROMPT, {"--RESPONSE--": self.response, "--ERROR--": error_msg}
        )


def prompt_execute():
    while True:
        print("EXECUTE?\n(Y/N) > ", end="", flush=True)
        user_input = getch.getch()
        print()
        if user_input.lower() == "y":
            return True
        elif user_input.lower() == "n":
            return False


def results_table(cursor):
    # SQLite result column names may not be unique, but PrettyTable needs them to be
    columns = make_unique([description[0] for description in cursor.description])
    table = PrettyTable(columns)
    for row in cursor.fetchall():
        table.add_row(row)
    return table


def make_unique(lst):
    count_dict = Counter(lst)
    unique_count = {}
    result = []

    for item in lst:
        if count_dict[item] > 1:
            if item in unique_count:
                unique_count[item] += 1
            else:
                unique_count[item] = 1
            result.append(f"{item}{unique_count[item]}")
        else:
            result.append(item)

    return result
