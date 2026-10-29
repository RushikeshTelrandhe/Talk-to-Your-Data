import csv
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'ecommerce.db')

SCHEMA = {
    'customers': """
        CREATE TABLE IF NOT EXISTS customers (
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
        )
    """,
    'products': """
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
        )
    """,
    'orders': """
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
        )
    """,
}

CSV_FILES = {
    'customers': 'customers.csv',
    'products': 'products.csv',
    'orders': 'orders.csv',
}


def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        for ddl in SCHEMA.values():
            conn.execute(ddl)

        for table, filename in CSV_FILES.items():
            path = os.path.join(BASE_DIR, filename)
            if not os.path.exists(path):
                print(f"  {table}: skipped, {filename} not found")
                continue

            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                print(f"  {table}: already populated ({count} rows)")
                continue

            with open(path, newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))

            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            usable = [c for c in columns if rows and c in rows[0]]
            placeholders = ','.join('?' * len(usable))
            sql = f"INSERT INTO {table} ({','.join(usable)}) VALUES ({placeholders})"
            values = [[None if row[c] == '' else row[c] for c in usable] for row in rows]

            conn.executemany(sql, values)
            print(f"  {table}: loaded {len(values)} rows")

        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    main()
