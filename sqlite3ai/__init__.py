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
            tables and their relationships, in about 100 words total.
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
            consist of SQL with no surrounding text or Markdown formatting, using only
            syntax and functions supported by SQLite3,
            If you cannot fulfill the user's intention for any reason, then provide a
            brief text explanation, without apology or other extraneous chatter.
            Importantly, your SQL must never alter or delete anything in the database,
            even if the user so demands.
        """,
    },
    {
        "role": "user",
        "content": """
            Assist me writing an SQL query for my SQLite3 database.
            I will input your responses directly into SQLite3, so I require each
            response to consist of one SQL query, with no surrounding text or Markdown
            formatting, using only syntax and functions supported by SQLite3.
            If a query is expected to yield multiple result rows, then set limit 25
            unless I clearly request otherwise.
            You may include short SQL inline comment lines starting with -- but only to
            give me brief hints about tricky or unusual parts.
            You may use common table expressions if required or to make the SQL much
            easier for me to understand.
            Due to the risk of infinite loop, don't use a recursive CTE unless
            absolutely required to fulfill my intent.
            I only want to query my database; if my input seems to suggest altering or
            deleting anything, then you must reject it.

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

            Output format: one SQL query with no surrounding text or Markdown
            formatting, using only SQL syntax and functions supported by SQLite3.
            No apology or other extraneous chatter.
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
        describe_schema(args.model, args.dbfn, schema)

        # enter main REPL
        return main_repl(
            args.model, dbc, schema, yes=args.yes, max_revisions=args.revisions
        )


def main_repl(model, dbc, schema, yes=False, max_revisions=3):
    # main REPL for separate queries until Ctrl+C/Ctrl+D
    first = True
    try:
        while True:
            # get user intent
            intent = user_intent(first)
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
                with spinner(
                    "Generating SQL"
                    if attempts == 1
                    else f"Regenerating SQL (attempt {attempts}/{max_revisions})"
                ):
                    # generate AI SQL
                    ai_sql = sql_prompt.fetch()
                if is_ai_refusal(ai_sql):
                    # AI refused user intent
                    print("\n" + textwrap.fill(ai_sql, width=88) + "\n")
                    break

                print("\n" + ai_sql + "\n")
                if yes or prompt_execute():
                    try:
                        with spinner("Executing query"):
                            # Execute query & populate results table
                            cursor = dbc.cursor()
                            cursor.execute(ai_sql)
                            table = PrettyTable(
                                [description[0] for description in cursor.description]
                            )
                            for row in cursor.fetchall():
                                table.add_row(row)
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


def user_intent(first=False):
    # ask user for their query intent
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
        response = openai.ChatCompletion.create(
            model=self.model, messages=self.messages
        )
        self.response = response.choices[0].message.content
        # the AI sometimes puts its SQL inside a markdown ```code block``` with chatter
        # before and/or after (contrary to our repeated instructions)
        lines = self.response.splitlines()
        ticks = [i for i, line in enumerate(lines) if line.strip() == "```"]
        if len(ticks) != 2 or ticks[1] - ticks[0] <= 1:
            return self.response.strip()
        lines = lines[(ticks[0] + 1) : ticks[1]]
        return "\n".join(lines).strip()

    def revise(self, error_msg):
        # prepare prompt to revise the previous response given error_msg.
        # to test this path, try entering:
        #   an interesting query of your choice. on your first response, deliberately
        #   introduce an error in your SQL. then I'll ask you to fix it.
        assert self.messages and self.messages[-1]["role"] == "user"
        self.messages += prepare_prompt(
            REVISE_PROMPT, {"--RESPONSE--": self.response, "--ERROR--": error_msg}
        )


def is_ai_refusal(message):
    # heuristic: detect a plain-English response from the AI, contrary to the
    # instruction for SQL only.
    # it mainly does this when refusing the user request to do something forbidden
    # (e.g. drop database)
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

# some notation to ask general questions about schema
# Classifier: is the user input expressing an intended query or
# is it a general question about the schema?
