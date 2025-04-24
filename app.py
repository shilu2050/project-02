import os
import sqlite3
from flask import Flask, render_template, request, redirect, session, url_for, g
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

from src.helper import download_hugging_face_embeddings
from langchain_pinecone import PineconeVectorStore
from openai import OpenAI

# Initialize app and env
app = Flask(__name__)
app.secret_key = "supersecretkey"
load_dotenv()

# Database setup
DATABASE = "users.db"
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        """)
        db.commit()

# Load API keys
PINECONE_API_KEY = os.environ.get('PINECONE_API_KEY')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY

# Embeddings & model setup
embeddings = download_hugging_face_embeddings()
index_name = "medicalbot"
docsearch = PineconeVectorStore.from_existing_index(index_name=index_name, embedding=embeddings)
retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k": 3})
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)

# ---------------------- ROUTES ----------------------

@app.route("/")
def index():
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user"] = username
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        hashed_pw = generate_password_hash(password)
        db = get_db()
        try:
            db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            db.commit()
            return redirect("/login")
        except sqlite3.IntegrityError:
            return "Username already taken!", 409
    return render_template("register.html")

@app.route("/forgot")
def forgot():
    return render_template("forgot.html")

@app.route("/dashboard")
def dashboard():
    if "user" in session:
        return render_template("dashboard.html", username=session["user"])
    return redirect("/login")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")

@app.route("/chat")
def chat_page():
    if "user" in session:
        return render_template("chat.html")
    return redirect("/login")

@app.route("/get", methods=["POST"])
def chat():
    msg = request.form["msg"]
    input_text = msg

    retrieved_docs = retriever.invoke(input_text)
    context = "\n\n".join([doc.page_content for doc in retrieved_docs])

    system_prompt = (
        "You are an assistant for question-answering tasks. "
        "Use the following pieces of retrieved context to answer "
        "the question. If you don't know the answer, say that you "
        "don't know. Use three sentences maximum and keep the "
        "answer concise.\n\n" + context
    )

    completion = client.chat.completions.create(
        extra_body={},
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text}
        ]
    )

    return completion.choices[0].message.content

# ---------------------- INIT ----------------------

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
