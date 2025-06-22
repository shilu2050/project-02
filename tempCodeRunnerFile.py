import os
import sqlite3
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from flask import Flask, render_template, request, redirect, session, url_for, g
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from src.helper import download_hugging_face_embeddings
from langchain_pinecone import PineconeVectorStore
from openai import OpenAI
from itsdangerous import URLSafeTimedSerializer
# Secret key should already be set for Flask



# Initialize app and env
app = Flask(__name__)
app.secret_key = "supersecretkey"
serializer = URLSafeTimedSerializer(app.secret_key)
load_dotenv()

# Mail config
app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=os.environ.get("EMAIL_USER"),  # Your Gmail/App Email
    MAIL_PASSWORD=os.environ.get("EMAIL_PASS")   # Use App Password if using Gmail
)

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)


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
        db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("EMAIL_USER")


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

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form["email"]
        db = get_db()
        
        user = db.execute("SELECT * FROM users WHERE username = ?", (email,)).fetchone()
        if user:
            token = serializer.dumps(email, salt='reset-salt')
            reset_link = url_for('reset_password', token=token, _external=True)

            
            msg = Message(
                subject="Reset Your Password",
                sender=app.config["MAIL_USERNAME"],
                recipients=[email]
            )

            msg.body = f"Hi,\nClick the link to reset your password: {reset_link}"
            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
              <h2>Password Reset Requested</h2>
              <p>Hello,</p>
              <p>We received a request to reset your password. Click the button below to continue:</p>
              <p>
                <a href="{reset_link}" style="background-color: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Reset Password</a>
              </p>
              <p>If you didn’t request this, you can safely ignore this email.</p>
              <br>
              <p>Thanks,<br>YourApp Team</p>
            </body>
            </html>
            """
            mail.send(msg)

            return "Reset link sent to your email."
        else:
            return "No user found with that email."
    return render_template("forgot.html")

@app.route("/reset/<token>", methods=["GET", "POST"])
 
def reset_password(token):
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600)  # 1 hour
    except Exception as e:
        return "Reset link is invalid or has expired."

    if request.method == 'POST':
        new_password = request.form['password']
        hashed_pw = generate_password_hash(new_password)
        db = get_db()
        db.execute("UPDATE users SET password = ? WHERE username = ?", (hashed_pw, email))
        db.commit()
        return redirect("/login")

    return render_template("reset.html", token=token)



@app.route("/dashboard")
def dashboard():
    if "user" in session:
        return render_template("dashboard.html", username=session["user"])
    return redirect("/login")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")
@app.route("/history")
def history():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    rows = db.execute(
        "SELECT question, answer, timestamp FROM chat_history WHERE username = ? ORDER BY timestamp DESC",
        (username,)
    ).fetchall()

    history = [dict(row) for row in rows]

    return render_template("history.html", history=history)


@app.route("/chat")
def chat_page():
    if "user" in session:
        return render_template("chat.html")
    return redirect("/login")

@app.route("/get", methods=["POST"])
def chat():
    if "user" not in session:
        return "Unauthorized", 401

    username = session["user"]
    msg = request.form["msg"]
    input_text = msg

    # Retrieve docs and prepare prompt
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
    answer = completion.choices[0].message.content

    # Save chat history
    db = get_db()
    db.execute(
        "INSERT INTO chat_history (username, question, answer) VALUES (?, ?, ?)",
        (username, input_text, answer)
    )
    db.commit()

    return answer


# ---------------------- INIT ----------------------

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
