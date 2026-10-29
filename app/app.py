from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import requests
import json
import os
import re
import uuid

import dataset_tools

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, instance_path=os.path.join(BASE_DIR, 'instance'))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024

DEFAULT_DB_PATH = os.path.join(BASE_DIR, 'ecommerce.db')
DATASETS_DIR = os.path.join(app.instance_path, 'datasets')
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_RESULT_ROWS = 5000
DEFAULT_LLM_BASE_URL = 'https://api.openai.com/v1'
DEFAULT_LLM_MODEL = 'gpt-4o-mini'
LLM_CREDENTIALS = {}

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Dataset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    source_type = db.Column(db.String(30), nullable=False, default='csv')
    source_ref = db.Column(db.Text, default='')
    tables_json = db.Column(db.Text, nullable=False, default='[]')
    total_rows = db.Column(db.Integer, default=0)
    db_filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


db_ready = False


@app.before_request
def _ensure_tables():
    global db_ready
    if not db_ready:
        os.makedirs(app.instance_path, exist_ok=True)
        db.create_all()
        os.makedirs(DATASETS_DIR, exist_ok=True)
        db_ready = True


class DatasetError(Exception):
    pass


def _require_user():
    return session.get('user_id')


def _require_user_response():
    if not _require_user():
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _user_dataset_dir(user_id):
    return os.path.join(DATASETS_DIR, str(user_id))


def _dataset_path(dataset):
    filename = os.path.basename(dataset.db_filename)
    return os.path.join(_user_dataset_dir(dataset.user_id), filename)


def _readonly_connection(db_path):
    uri = 'file:' + db_path.replace('\\', '/') + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _serialize_dataset(dataset):
    try:
        tables = json.loads(dataset.tables_json or '[]')
    except ValueError:
        tables = []
    return {
        'id': dataset.id,
        'name': dataset.name,
        'source_type': dataset.source_type,
        'source_ref': dataset.source_ref or '',
        'tables': tables,
        'table_count': len(tables),
        'total_rows': dataset.total_rows or 0,
        'active': session.get('dataset_id') == dataset.id,
        'created_at': dataset.created_at.isoformat() if dataset.created_at else None,
    }


def _active_dataset():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        return None
    dataset = Dataset.query.filter_by(
        id=dataset_id, user_id=session.get('user_id')
    ).first()
    if not dataset:
        session.pop('dataset_id', None)
    return dataset


def _latest_dataset(user_id):
    return (
        Dataset.query.filter_by(user_id=user_id)
        .order_by(Dataset.created_at.desc(), Dataset.id.desc())
        .first()
    )


def _create_dataset(user_id, name, source_type, source_ref, payloads):
    os.makedirs(_user_dataset_dir(user_id), exist_ok=True)
    filename = f'{uuid.uuid4().hex}.db'
    db_path = os.path.join(_user_dataset_dir(user_id), filename)

    try:
        tables = dataset_tools.load_payloads_into_db(db_path, payloads)
    except Exception:
        if os.path.exists(db_path):
            os.remove(db_path)
        raise

    total_rows = sum(table.get('row_count', 0) for table in tables)
    dataset = Dataset(
        user_id=user_id,
        name=(name or 'Dataset')[:120],
        source_type=source_type,
        source_ref=source_ref or '',
        tables_json=json.dumps(tables),
        total_rows=total_rows,
        db_filename=filename,
    )
    db.session.add(dataset)
    db.session.commit()
    session['dataset_id'] = dataset.id
    return dataset


def _clean_sql(raw):
    sql = (raw or '').strip()
    if '```' in sql:
        blocks = re.findall(r'```(?:sql)?\s*(.*?)```', sql, re.S | re.I)
        if blocks:
            sql = blocks[0].strip()

    match = re.search(r'\b(select|with)\b', sql, re.I)
    if match and match.start() > 0:
        sql = sql[match.start():]

    sql = sql.strip()
    while sql.endswith(';'):
        sql = sql[:-1].rstrip()
    return sql


def _validate_read_sql(sql):
    if not sql:
        raise DatasetError('The model did not return a SQL query.')
    if ';' in sql:
        raise DatasetError('Only a single SQL statement is allowed.')
    if not re.match(r'^(select|with)\b', sql, re.I):
        raise DatasetError('Only SELECT queries are allowed.')


def _execute_query(db_path, sql):
    _validate_read_sql(sql)
    if not os.path.exists(db_path):
        raise DatasetError(
            'The database file is missing. Upload a dataset or run load_data.py.'
        )

    connection = _readonly_connection(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        columns = [item[0] for item in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        truncated = len(fetched) > MAX_RESULT_ROWS
        rows = [dict(row) for row in fetched[:MAX_RESULT_ROWS]]
        return columns, rows, truncated
    finally:
        connection.close()


def _default_schema_ddl():
    return """CREATE TABLE IF NOT EXISTS customers (
    customer_id INT PRIMARY KEY,
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    email VARCHAR(50),
    phone_number VARCHAR(50),
    address VARCHAR(50),
    city VARCHAR(50),
    country VARCHAR(50),
    postal_code VARCHAR(50),
    loyalty_points INT
);

CREATE TABLE IF NOT EXISTS products (
    product_id INT PRIMARY KEY,
    product_name TEXT,
    description TEXT,
    price DECIMAL(10,2),
    discount_percentage DECIMAL(5,2),
    category VARCHAR(50),
    brand TEXT,
    stock_quantity INT,
    color VARCHAR(50),
    size VARCHAR(20),
    weight DECIMAL(5,2),
    dimensions TEXT,
    release_date DATE,
    rating DECIMAL(3,1),
    reviews_count INT,
    seller_name TEXT,
    seller_rating DECIMAL(3,1),
    seller_reviews_count INT,
    shipping_method VARCHAR(20),
    shipping_cost DECIMAL(6,2)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id INT PRIMARY KEY,
    customer_id INT,
    product_id INT,
    quantity INT,
    unit_price DECIMAL(10,2),
    total_price DECIMAL(10,2),
    order_date DATE,
    shipping_address VARCHAR(255),
    payment_method VARCHAR(20),
    status VARCHAR(20),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);"""


def _dataset_schema_ddl(dataset):
    tables = json.loads(dataset.tables_json or '[]')
    blocks = []
    notes = []
    for table in tables:
        columns = ',\n    '.join(
            f'"{column["name"]}" {column["type"]}'
            for column in table.get('columns', [])
        )
        blocks.append(f'CREATE TABLE "{table["table"]}" (\n    {columns}\n);')
        notes.append(f'"{table["table"]}" has {table.get("row_count", 0)} rows.')
    return '\n\n'.join(blocks), ' '.join(notes)


def _build_schema_prompt(dataset):
    if dataset:
        ddl, notes = _dataset_schema_ddl(dataset)
        context = (
            f'The user is working with their uploaded dataset named "{dataset.name}". '
            f'The database includes the following tables. {notes}'
        )
    else:
        ddl = _default_schema_ddl()
        context = (
            'The user is working with a relational database for an e-commerce '
            'platform. The database includes three main tables: customers, '
            'products, and orders.'
        )

    return f"""You are a highly skilled Text-to-SQL translator with expertise in SQL syntax, database schema interpretation, and natural language understanding. You generate syntactically correct and semantically accurate SQL queries based on user input and a given database schema.

{context}

Schema:
{ddl}

Rules:
- Only generate SQL queries, no explanations
- Use proper SQL syntax for SQLite
- Use only the tables and columns listed in the schema above
- Return only a single SELECT statement (WITH ... SELECT is allowed), no markdown code blocks
- Quote identifiers with double quotes when needed
- Be precise and efficient
"""


def _get_llm_credentials():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return LLM_CREDENTIALS.get(user_id)


def _mask_secret(secret):
    if not secret:
        return ''
    if len(secret) <= 8:
        return '*' * len(secret)
    return secret[:4] + '*' * (len(secret) - 8) + secret[-4:]


def _normalize_base_url(value):
    base_url = (value or '').strip().rstrip('/')
    if not base_url:
        return DEFAULT_LLM_BASE_URL
    if not re.match(r'^https?://', base_url, re.I):
        raise DatasetError('The API base URL must start with http:// or https://.')
    return base_url


def _llm_settings_payload(credentials):
    if not credentials:
        return {
            'enabled': False,
            'base_url': DEFAULT_LLM_BASE_URL,
            'model': DEFAULT_LLM_MODEL,
            'masked_key': '',
        }
    return {
        'enabled': True,
        'base_url': credentials['base_url'],
        'model': credentials['model'],
        'masked_key': _mask_secret(credentials['api_key']),
    }


def _generate_sql_with_external_llm(credentials, system_prompt, user_query):
    base_url = credentials['base_url'].rstrip('/')
    try:
        response = requests.post(
            f'{base_url}/chat/completions',
            headers={
                'Authorization': f"Bearer {credentials['api_key']}",
                'Content-Type': 'application/json',
            },
            json={
                'model': credentials['model'],
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_query},
                ],
                'temperature': 0.1,
            },
            timeout=120,
        )
    except requests.exceptions.RequestException:
        raise DatasetError(
            'Could not reach the external API. Check the base URL and your connection.'
        )

    if response.status_code in (401, 403):
        raise DatasetError(
            'The API key or token was rejected. Ask the authorized user for a valid one.'
        )
    if response.status_code != 200:
        raise DatasetError(
            f'The external API returned an error ({response.status_code}).'
        )

    try:
        return response.json()['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError):
        raise DatasetError('The external API returned an unexpected response.')


@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            latest = _latest_dataset(user.id)
            if latest:
                session['dataset_id'] = latest.id
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password')

    return render_template('login.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']

        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return render_template('signup.html')

        if User.query.filter_by(email=email).first():
            flash('Email already exists')
            return render_template('signup.html')

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('Registration successful! Please login.')
        return redirect(url_for('login'))

    return render_template('signup.html')


@app.route('/logout')
def logout():
    LLM_CREDENTIALS.pop(session.get('user_id'), None)
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/llm/settings', methods=['GET'])
def get_llm_settings():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized
    return jsonify(_llm_settings_payload(_get_llm_credentials()))


@app.route('/api/llm/settings', methods=['POST'])
def save_llm_settings():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    data = request.get_json(silent=True) or {}
    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        return jsonify({'error': 'Enter an API key or token.'}), 400

    try:
        base_url = _normalize_base_url(data.get('base_url'))
    except DatasetError as exc:
        return jsonify({'error': str(exc)}), 400

    credentials = {
        'api_key': api_key,
        'base_url': base_url,
        'model': (data.get('model') or '').strip() or DEFAULT_LLM_MODEL,
    }
    LLM_CREDENTIALS[session['user_id']] = credentials
    return jsonify(_llm_settings_payload(credentials))


@app.route('/api/llm/settings', methods=['DELETE'])
def delete_llm_settings():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized
    LLM_CREDENTIALS.pop(session['user_id'], None)
    return jsonify(_llm_settings_payload(None))


@app.route('/api/datasets', methods=['GET'])
def list_datasets():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    datasets = (
        Dataset.query.filter_by(user_id=session['user_id'])
        .order_by(Dataset.created_at.desc(), Dataset.id.desc())
        .all()
    )
    return jsonify({
        'datasets': [_serialize_dataset(dataset) for dataset in datasets],
        'active_id': session.get('dataset_id'),
    })


@app.route('/api/datasets/upload', methods=['POST'])
def upload_dataset():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': 'Please choose at least one CSV file.'}), 400

    payloads = []
    for file_storage in files:
        filename = secure_filename(file_storage.filename) or 'dataset.csv'
        if not filename.lower().endswith(('.csv', '.tsv')):
            return jsonify({'error': f'{filename} is not a CSV file.'}), 400
        data = file_storage.read()
        if not data:
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({'error': f'{filename} is larger than 100 MB.'}), 413
        payloads.append((os.path.splitext(filename)[0], data))

    if not payloads:
        return jsonify({'error': 'The uploaded files were empty.'}), 400

    name = (request.form.get('name') or '').strip()
    if not name:
        name = payloads[0][0]
        if len(payloads) > 1:
            name = f'{name} + {len(payloads) - 1} more'

    source_type = 'csv' if len(payloads) == 1 else 'csv_bundle'
    try:
        dataset = _create_dataset(
            session['user_id'], name, source_type, '', payloads
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400

    return jsonify({'dataset': _serialize_dataset(dataset)})


@app.route('/api/datasets/import', methods=['POST'])
def import_dataset():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    name = (data.get('name') or '').strip()

    try:
        source_type, source_ref, payloads, suggested_name = dataset_tools.fetch_from_url(url)
        dataset = _create_dataset(
            session['user_id'],
            name or suggested_name,
            source_type,
            source_ref,
            payloads,
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400

    return jsonify({'dataset': _serialize_dataset(dataset)})


@app.route('/api/datasets/<int:dataset_id>/activate', methods=['POST'])
def activate_dataset(dataset_id):
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    dataset = Dataset.query.filter_by(
        id=dataset_id, user_id=session['user_id']
    ).first()
    if not dataset:
        return jsonify({'error': 'Dataset not found.'}), 404

    session['dataset_id'] = dataset.id
    return jsonify({'dataset': _serialize_dataset(dataset)})


@app.route('/api/datasets/default', methods=['POST'])
def use_default_dataset():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized
    session.pop('dataset_id', None)
    return jsonify({'ok': True})


@app.route('/api/datasets/<int:dataset_id>/overview', methods=['GET'])
def dataset_overview(dataset_id):
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    dataset = Dataset.query.filter_by(
        id=dataset_id, user_id=session['user_id']
    ).first()
    if not dataset:
        return jsonify({'error': 'Dataset not found.'}), 404

    tables = json.loads(dataset.tables_json or '[]')
    db_path = _dataset_path(dataset)
    try:
        connection = _readonly_connection(db_path)
        try:
            for table in tables:
                table_name = table['table']
                cursor = connection.execute(
                    f'SELECT * FROM "{table_name}" LIMIT 5'
                )
                columns = [item[0] for item in cursor.description] if cursor.description else []
                table['sample_columns'] = columns
                table['sample_rows'] = [list(row) for row in cursor.fetchall()]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return jsonify({'error': f'Could not read dataset: {exc}'}), 400

    return jsonify({'dataset': _serialize_dataset(dataset), 'tables': tables})


@app.route('/api/datasets/<int:dataset_id>', methods=['DELETE'])
def delete_dataset(dataset_id):
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    dataset = Dataset.query.filter_by(
        id=dataset_id, user_id=session['user_id']
    ).first()
    if not dataset:
        return jsonify({'error': 'Dataset not found.'}), 404

    db_path = _dataset_path(dataset)
    if session.get('dataset_id') == dataset.id:
        session.pop('dataset_id', None)

    db.session.delete(dataset)
    db.session.commit()

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass

    return jsonify({'ok': True})


@app.route('/api/query', methods=['POST'])
def query():
    unauthorized = _require_user_response()
    if unauthorized:
        return unauthorized

    data = request.get_json(silent=True) or {}
    user_query = (data.get('query') or '').strip()
    if not user_query:
        return jsonify({'error': 'Please enter a question.'}), 400

    dataset = _active_dataset()
    db_path = _dataset_path(dataset) if dataset else DEFAULT_DB_PATH
    schema_prompt = _build_schema_prompt(dataset)
    credentials = _get_llm_credentials()

    try:
        if credentials:
            raw_sql = _generate_sql_with_external_llm(
                credentials, schema_prompt, user_query
            )
            provider = 'external'
        else:
            model_name = os.environ.get('OLLAMA_MODEL', 'nemotron-3.5-bolt:e4b')
            response = requests.post('http://127.0.0.1:11434/api/generate', json={
                'model': model_name,
                'prompt': f"{schema_prompt}\n\nUser question: {user_query}\n\nSQL Query:",
                'stream': False,
                'options': {
                    'temperature': 0.1,
                    'top_p': 0.9
                }
            }, timeout=600)

            if response.status_code != 200:
                return jsonify({'error': f'Ollama error: {response.text}'}), 500

            raw_sql = response.json().get('response', '')
            provider = 'ollama'

        sql_query = _clean_sql(raw_sql)
        columns, rows, truncated = _execute_query(db_path, sql_query)

        return jsonify({
            'sql': sql_query,
            'columns': columns,
            'results': rows,
            'truncated': truncated,
            'dataset': dataset.name if dataset else 'Sample e-commerce data',
            'provider': provider,
        })
    except requests.exceptions.RequestException:
        return jsonify({
            'error': 'Could not reach Ollama at http://127.0.0.1:11434. '
                     'Make sure Ollama is running.'
        }), 503
    except DatasetError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.errorhandler(413)
def too_large(_error):
    return jsonify({'error': 'Upload is too large. Keep it under 100 MB.'}), 413


if __name__ == '__main__':
    os.makedirs(app.instance_path, exist_ok=True)
    with app.app_context():
        db.create_all()
    os.makedirs(DATASETS_DIR, exist_ok=True)
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=5000)
