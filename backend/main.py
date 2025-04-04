from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from db import get_connection, get_table_schema, list_all_tables
from prompt import generate_sql_query, generate_data_dictionary_prompt
import requests
import pandas as pd
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import pairwise_distances
import numpy as np
import re
from typing import List, Dict

app = FastAPI()

# Allow frontend (Streamlit) to access the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LLM_URL = "http://192.168.1.142:1234/v1/chat/completions"

def get_diverse_sample(df: pd.DataFrame, n=10):
    if len(df) <= n:
        return df  # not enough rows to sample, return all

    df_copy = df.copy()
    df_copy.fillna("N/A", inplace=True)

    for col in df_copy.columns:
        if df_copy[col].dtype == "object":
            le = LabelEncoder()
            df_copy[col] = le.fit_transform(df_copy[col].astype(str))

    distance_matrix = pairwise_distances(df_copy, metric='euclidean')
    selected_indices = [np.random.randint(len(df_copy))]

    for _ in range(n - 1):
        remaining = list(set(range(len(df_copy))) - set(selected_indices))
        if not remaining:
            break
        max_distances = distance_matrix[remaining][:, selected_indices].mean(axis=1)
        next_index = remaining[np.argmax(max_distances)]
        selected_indices.append(next_index)

    return df.iloc[selected_indices]

class QueryRequest(BaseModel):
    table_name: str
    question: str

@app.get("/tables")
def get_tables():
    return list_all_tables()

class GenerateSQLRequest(BaseModel):
    table_name: str
    question: str
    data_dictionary: list  # List of dicts: [{"Column": ..., "Description": ...}]
    sample_data: list      # List of sample rows as dicts

@app.post("/generate-sql")
def generate_sql(req: GenerateSQLRequest):
    dict_lines = [f"- {col['Column']}: {col['Description']}" for col in req.data_dictionary]
    dict_section = "\n".join(dict_lines)

    df_sample = pd.DataFrame(req.sample_data)
    diverse_sample = get_diverse_sample(df_sample, n=10)
    sample_lines = [str(row) for _, row in diverse_sample.iterrows()]
    sample_section = "\n".join(sample_lines)

    prompt = f"""
    You are an assistant that generates SQL queries from natural language.

    The user is asking a question about the `{req.table_name}` table.

    ### Data Dictionary:
    {dict_section}

    ### Sample Data:
    {sample_section}

    User's question:
    \"\"\"{req.question}\"\"\"

    Generate a SQL SELECT query that best answers the question.
    Use only the `{req.table_name}` table. Do not return explanations—only the SQL.
    """

    response = requests.post(LLM_URL, headers={"Content-Type": "application/json"}, json={
        "model": "qwen2.5-7b-instruct-1m",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that generates SQL queries from natural language."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    })
    response.raise_for_status()

    # Clean the raw SQL
    sql_raw = response.json()["choices"][0]["message"]["content"]
    sql_clean = sql_raw.strip()

    if sql_clean.startswith("```"):
        lines = sql_clean.splitlines()
        lines = [line for line in lines if not line.strip().startswith("```")]
        sql_clean = "\n".join(lines).strip()

    return {"sql": sql_clean, "prompt": prompt}


class DictionaryRequest(BaseModel):
    table_name: str

@app.post("/data-dictionary")
def get_data_dictionary(request: DictionaryRequest):
    schema = get_table_schema(request.table_name)
    prompt = generate_data_dictionary_prompt(request.table_name, schema)

    response = requests.post(LLM_URL, headers={"Content-Type": "application/json"}, json={
        "model": "qwen2.5-7b-instruct-1m",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that describes database tables."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    })
    response.raise_for_status()
    result = response.json()["choices"][0]["message"]["content"]
    print("LLM Response:\n", result)  # <- Debugging line

    entries = []
    for line in result.strip().splitlines():
        match = re.match(r"-\s*`?(\w+)`?\s*:\s*(.+)", line)
        if match:
            col, desc = match.groups()
            entries.append({
                "Column": col,
                "Description": desc
            })

    return {"dictionary": entries}

@app.get("/sample-data/{table_name}")
def get_sample_data(table_name: str):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM {table_name} LIMIT 100;")
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    finally:
        cur.close()
        conn.close()

    df = pd.DataFrame(rows, columns=columns)

    # Apply diverse sampling (10 rows)
    if len(df) > 10:
        df = get_diverse_sample(df, n=10)

    return df.to_dict(orient="records")

class RunSQLRequest(BaseModel):
    sql: str

@app.post("/run-sql")
def run_sql(request: RunSQLRequest):
    sql = request.sql.strip().lower()

    FORBIDDEN = ["drop", "delete", "insert", "update", "alter", "truncate"]
    if any(word in sql for word in FORBIDDEN):
        return JSONResponse(
            content={"error": "Query contains forbidden keywords."},
            status_code=403
        )

    # Restrict to SELECT statements only
    if not sql.startswith("select"):
        return JSONResponse(
            content={"error": "Only SELECT statements are allowed."},
            status_code=403
        )

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(request.sql)
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        cur.close()
        conn.close()
        return {"columns": columns, "rows": rows}
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)

@app.post("/generate-sample-questions")
def generate_sample_questions(req: GenerateSQLRequest):
    dict_lines = [f"- {col['Column']}: {col['Description']}" for col in req.data_dictionary]
    dict_section = "\n".join(dict_lines)

    df_sample = pd.DataFrame(req.sample_data)
    if not df_sample.empty:
        diverse_sample = get_diverse_sample(df_sample, n=10)
        sample_lines = [str(row) for _, row in diverse_sample.iterrows()]
        sample_section = "\n".join(sample_lines)
    else:
        sample_section = "(No sample data provided)"

    prompt = f"""
    You are a helpful assistant that suggests example questions users might ask about the `{req.table_name}` table.

    ### Data Dictionary:
    {dict_section}

    ### Sample Data:
    {sample_section}

    Generate 3 example natural language questions that could be answered using a SQL SELECT query on the `{req.table_name}` table.
    Do not include any explanations—only the questions, each as a separate bullet point.
    """

    response = requests.post(LLM_URL, headers={"Content-Type": "application/json"}, json={
        "model": "qwen2.5-7b-instruct-1m",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that suggests natural language queries for SQL generation."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    })
    response.raise_for_status()

    raw_text = response.json()["choices"][0]["message"]["content"]
    questions = [q.strip("•- ").strip() for q in raw_text.strip().splitlines() if q.strip()]
    return {"questions": questions}

class DescribeResultsRequest(BaseModel):
    sql: str
    rows: List[Dict]

@app.post("/describe-results")
def describe_results(req: DescribeResultsRequest):
    df = pd.DataFrame(req.rows)
    sample_lines = df.head(10).to_string(index=False)

    prompt = f"""
    You are a data analyst assistant.

    Given the following SQL query and sample results from that query, provide a short natural language summary of what the query result is showing. Do not explain SQL — just describe the result as if speaking to a non-technical user.

    ### SQL Query:
    {req.sql}

    ### Sample Results (top 10 rows):
    {sample_lines}

    Summarize the data shown above in 1–2 sentences.
    """

    response = requests.post(LLM_URL, headers={"Content-Type": "application/json"}, json={
        "model": "qwen2.5-7b-instruct-1m",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant who explains SQL query results to business users."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    })
    response.raise_for_status()

    summary = response.json()["choices"][0]["message"]["content"].strip()
    return {"summary": summary}

@app.post("/suggest-chart")
def suggest_chart(req: DescribeResultsRequest):
    df = pd.DataFrame(req.rows)
    sample_lines = df.head(10).to_string(index=False)

    prompt = f"""
    You are a data visualization assistant.

    Given the following SQL query and sample result data, decide whether a chart is useful.

    If a chart makes sense, suggest:
    - A chart type (bar, line, pie, scatter, etc)
    - A column for the X-axis
    - A column for the Y-axis

    If a chart does **not** make sense (e.g., text-heavy, too few rows, non-numeric data), say:
    Chart Type: None

    ### SQL Query:
    {req.sql}

    ### Sample Results (top 10 rows):
    {sample_lines}

    Respond in this exact format:
    Chart Type: <bar, line, pie, scatter, none>
    X-Axis: <column name or 'None'>
    Y-Axis: <column name or 'None'>
    """

    response = requests.post(LLM_URL, headers={"Content-Type": "application/json"}, json={
        "model": "qwen2.5-7b-instruct-1m",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant for visualizing SQL result data."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    })
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]

    # Parse response into parts
    lines = content.strip().splitlines()
    chart_type = next((line.split(":")[1].strip() for line in lines if line.lower().startswith("chart type")), None)
    x_axis = next((line.split(":")[1].strip() for line in lines if line.lower().startswith("x-axis")), None)
    y_axis = next((line.split(":")[1].strip() for line in lines if line.lower().startswith("y-axis")), None)

    # Normalize "none" axis values to None
    if x_axis and x_axis.lower() == "none":
        x_axis = None
    if y_axis and y_axis.lower() == "none":
        y_axis = None

    return {
        "chart_type": chart_type,
        "x_axis": x_axis,
        "y_axis": y_axis
    }
