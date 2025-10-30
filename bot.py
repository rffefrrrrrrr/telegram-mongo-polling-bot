import telebot
from telebot import types
import segno
from io import BytesIO
import json
from datetime import datetime, timedelta
import requests
import time
import threading
import logging
from decimal import Decimal, getcontext, InvalidOperation
import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
from flask import Flask, request

# --- Configuration and Setup ---
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Set decimal precision for financial calculations
getcontext().prec = 50 

# الحصول على التوكن وسلسلة الاتصال من متغيرات البيئة (ضروري لـ Render)
BOT_TOKEN = os.environ.get('BOT_TOKEN', '7678808636:AAH0pI0EDxYqSjMUhKiOTFWLo3TQT3qz2e8') # القيمة الثانية هي قيمة افتراضية/تجريبية
ADMIN_ID = int(os.environ.get('ADMIN_ID', 8129146878)) # !!! IMPORTANT: REPLACE THIS WITH YOUR ACTUAL TELEGRAM USER ID !!!

# MongoDB Connection String and Database Name
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb+srv://Sliman:Sliman@cluster0.meyh75w.mongodb.net/?appName=Cluster0')
DB_NAME = os.environ.get('DB_NAME', 'telegram_bot_db') # يمكنك تغيير هذا الاسم

# Initialize the bot
try:
    bot = telebot.TeleBot(BOT_TOKEN)
except Exception as e:
    logging.error(f"Failed to initialize Telegram Bot: {e}")
    raise

# Global state management
user_sessions = {}
user_state = {} 

# --- MongoDB Database Functions ---
db_lock = threading.Lock()
mongo_client = None
db = None

def init_database():
    """Initializes the MongoDB connection and returns the database object."""
    global mongo_client, db
    try:
        # Connect to MongoDB
        mongo_client = MongoClient(MONGO_URI)
        mongo_client.admin.command('ping') # Test connection
        db = mongo_client[DB_NAME]
        logging.info("Successfully connected to MongoDB.")
        
        # Ensure indexes for key fields (equivalent to UNIQUE/PRIMARY KEY in SQL)
        db.wallets.create_index("crypto_name", unique=True)
        db.products.create_index("id", unique=True) # Use 'id' as primary key equivalent
        db.products.create_index("product_name", unique=True)
        db.transactions.create_index("txid", unique=True, sparse=True)
        db.users.create_index("id", unique=True) # Telegram user ID
        db.used_txids.create_index("txid", unique=True)
        
        return db
    except ConnectionFailure as e:
        logging.error(f"MongoDB Connection Error: {e}")
        # The bot cannot run without a database connection
        exit(1)
    except OperationFailure as e:
        logging.error(f"MongoDB Operation Failure (Authentication/Permissions): {e}")
        exit(1)
    except Exception as e:
        logging.error(f"An unexpected error occurred during MongoDB initialization: {e}")
        exit(1)

# Initialize the database connection globally
db = init_database()

# Helper function to get next ID (since MongoDB doesn't have auto-increment)
def get_next_sequence_value(collection_name):
    """Gets the next sequential ID for a collection."""
    with db_lock:
        try:
            # Find the next sequential ID.
            last_doc = db[collection_name].find_one(sort=[('id', -1)])
            return (last_doc['id'] if last_doc and 'id' in last_doc else 0) + 1
        except Exception as e:
            logging.error(f"Error getting next sequence value for {collection_name}: {e}")
            return 1

def add_wallet(crypto_name, address):
    """Adds or updates a wallet address."""
    with db_lock:
        try:
            db.wallets.update_one(
                {'crypto_name': crypto_name},
                {'$set': {'wallet_address': address, 'updated_at': datetime.now()}},
                upsert=True
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in add_wallet: {e}")

def get_wallets():
    """Retrieves all stored wallets."""
    with db_lock:
        try:
            wallets = db.wallets.find({})
            return {wallet['crypto_name']: wallet['wallet_address'] for wallet in wallets}
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_wallets: {e}")
            return {}

def get_product_by_id(product_id):
    """Retrieves an active product by ID."""
    with db_lock:
        try:
            # Convert product_id to integer if it's a string
            p_id = int(product_id) if isinstance(product_id, str) and product_id.isdigit() else product_id
            
            product = db.products.find_one({'id': p_id, 'status': 'active'})
            if product:
                return {
                    'id': product['id'], 
                    'name': product['product_name'], 
                    'price': Decimal(str(product['price'])), 
                    'type': product['product_type'], 
                    'has_stock': product.get('has_stock', 0)
                }
            return None
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_product_by_id: {e}")
            return None
        except ValueError:
            logging.error(f"Invalid product_id: {product_id}")
            return None

def update_product_stock_status(product_id, has_stock):
    """Updates the has_stock status for a product."""
    with db_lock:
        try:
            db.products.update_one(
                {'id': product_id},
                {'$set': {'has_stock': has_stock}}
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in update_product_stock_status: {e}")

def add_product(name, price, product_type, has_stock):
    """Adds a new product."""
    with db_lock:
        try:
            new_id = get_next_sequence_value('products')
            
            product_doc = {
                'id': new_id,
                'product_name': name,
                'price': float(price), # Store as float/double in MongoDB
                'product_type': product_type,
                'status': 'active',
                'has_stock': has_stock,
                'created_at': datetime.now()
            }
            db.products.insert_one(product_doc)
            return new_id
        except OperationFailure as e:
            if 'duplicate key error' in str(e):
                logging.warning(f"Attempted to add duplicate product name: {name}")
            else:
                logging.error(f"Error adding product: {e}")
            return None

def delete_product(product_id):
    """Deletes a product by setting its status to 'deleted'."""
    with db_lock:
        try:
            db.products.update_one(
                {'id': product_id},
                {'$set': {'status': 'deleted'}}
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in delete_product: {e}")

def get_products():
    """Retrieves all active products with stock count."""
    with db_lock:
        try:
            products_cursor = db.products.find({'status': 'active'})
            products = {}
            for product in products_cursor:
                pid = product['id']
                stock_count = get_stock_count(pid)
                
                has_stock = 1 if stock_count > 0 else 0
                
                products[str(pid)] = {
                    'name': product['product_name'], 
                    'price': Decimal(str(product['price'])), 
                    'type': product['product_type'], 
                    'has_stock': has_stock, 
                    'stock': stock_count
                }
            return products
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_products: {e}")
            return {}

def get_stock_count(product_id):
    """Gets the count of unused items in the stash for a product."""
    with db_lock:
        try:
            count = db.product_stash.count_documents({'product_id': product_id, 'is_used': 0})
            return count
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_stock_count: {e}")
            return 0

def add_stash_item(product_id, content, file_id=None, file_type=None):
    """Adds an item to the product stash and updates product stock status."""
    with db_lock:
        try:
            new_id = get_next_sequence_value('product_stash')
            
            stash_doc = {
                'id': new_id,
                'product_id': product_id,
                'content': content,
                'file_id': file_id,
                'file_type': file_type,
                'is_used': 0,
                'added_at': datetime.now()
            }
            db.product_stash.insert_one(stash_doc)
            
            update_product_stock_status(product_id, 1)
            return new_id
        except OperationFailure as e:
            logging.error(f"MongoDB error in add_stash_item: {e}")
            return None

def get_available_stash_item(product_id):
    """Gets one available stash item without marking it as used."""
    with db_lock:
        try:
            item = db.product_stash.find_one({'product_id': product_id, 'is_used': 0}, sort=[('added_at', 1)])
            if item:
                return {'id': item['id'], 'content': item['content'], 'file_id': item['file_id'], 'file_type': item['file_type']}
            return None
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_available_stash_item: {e}")
            return None

def mark_stash_item_used(stash_id):
    """Marks a stash item as used and checks if stock is depleted."""
    with db_lock:
        try:
            # 1. Mark as used
            result = db.product_stash.update_one(
                {'id': stash_id},
                {'$set': {'is_used': 1}}
            )
            
            if result.modified_count > 0:
                # 2. Check stock status
                item = db.product_stash.find_one({'id': stash_id}, {'product_id': 1})
                if item and 'product_id' in item:
                    product_id = item['product_id']
                    if get_stock_count(product_id) == 0:
                        update_product_stock_status(product_id, 0)
        except OperationFailure as e:
            logging.error(f"MongoDB error in mark_stash_item_used: {e}")

def unmark_stash_item_used(stash_id):
    """Unmarks a stash item (returns it to stock) and updates product stock status."""
    with db_lock:
        try:
            # 1. Unmark as used
            result = db.product_stash.update_one(
                {'id': stash_id},
                {'$set': {'is_used': 0}}
            )
            
            if result.modified_count > 0:
                # 2. Check stock status
                item = db.product_stash.find_one({'id': stash_id}, {'product_id': 1})
                if item and 'product_id' in item:
                    product_id = item['product_id']
                    update_product_stock_status(product_id, 1)
        except OperationFailure as e:
            logging.error(f"MongoDB error in unmark_stash_item_used: {e}")

def add_transaction(user_id, username, product_id, product_name, amount, crypto, txid, status, stash_id):
    """Adds a new transaction record."""
    with db_lock:
        try:
            transaction_doc = {
                'user_id': user_id,
                'username': username,
                'product_id': product_id,
                'product_name': product_name,
                'amount': float(amount),
                'crypto_type': crypto,
                'txid': txid,
                'status': status,
                'stash_id': stash_id,
                'created_at': datetime.now()
            }
            db.transactions.insert_one(transaction_doc)
            return True
        except OperationFailure as e:
            if 'duplicate key error' in str(e):
                logging.warning(f"Attempted to add duplicate transaction ID: {txid}")
                return False
            else:
                logging.error(f"Error adding transaction: {e}")
                return False

def get_transaction_by_txid(txid):
    """Retrieves a transaction record by its TXID."""
    with db_lock:
        try:
            return db.transactions.find_one({'txid': txid})
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_transaction_by_txid: {e}")
            return None

def update_transaction_status(txid, status):
    """Updates the status of a transaction."""
    with db_lock:
        try:
            db.transactions.update_one(
                {'txid': txid},
                {'$set': {'status': status, 'verified_at': datetime.now()}}
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in update_transaction_status: {e}")

def get_transaction_by_stash_id(stash_id):
    """Retrieves a transaction record by its stash ID."""
    with db_lock:
        try:
            return db.transactions.find_one({'stash_id': stash_id})
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_transaction_by_stash_id: {e}")
            return None

def get_pending_transactions():
    """Retrieves all pending transactions."""
    with db_lock:
        try:
            return list(db.transactions.find({'status': 'pending'}))
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_pending_transactions: {e}")
            return []

def add_user(user_id, username, first_name, last_name):
    """Adds a new user if they don't exist."""
    with db_lock:
        try:
            user_doc = {
                'id': user_id,
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'joined_at': datetime.now(),
                'total_purchases': 0,
                'total_spent': 0.0
            }
            db.users.update_one(
                {'id': user_id},
                {'$setOnInsert': user_doc},
                upsert=True
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in add_user: {e}")

def get_user_stats(user_id):
    """Retrieves user's purchase statistics."""
    with db_lock:
        try:
            user = db.users.find_one({'id': user_id})
            if user:
                return {
                    'joined_at': user['joined_at'], 
                    'total_purchases': user['total_purchases'], 
                    'total_spent': user['total_spent']
                }
            return None
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_user_stats: {e}")
            return None

def update_user_stats(user_id, purchase_amount):
    """Updates user's purchase count and total spent."""
    with db_lock:
        try:
            db.users.update_one(
                {'id': user_id},
                {
                    '$inc': {'total_purchases': 1, 'total_spent': float(purchase_amount)}
                }
            )
        except OperationFailure as e:
            logging.error(f"MongoDB error in update_user_stats: {e}")

def is_txid_used(txid):
    """Checks if a transaction ID has already been processed."""
    with db_lock:
        try:
            return db.used_txids.find_one({'txid': txid}) is not None
        except OperationFailure as e:
            logging.error(f"MongoDB error in is_txid_used: {e}")
            return False

def mark_txid_used(txid):
    """Marks a transaction ID as used."""
    with db_lock:
        try:
            db.used_txids.insert_one({'txid': txid, 'used_at': datetime.now()})
        except OperationFailure as e:
            logging.error(f"MongoDB error in mark_txid_used: {e}")

def get_all_users():
    """Retrieves all users."""
    with db_lock:
        try:
            return list(db.users.find({}))
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_all_users: {e}")
            return []

def get_all_products_admin():
    """Retrieves all products for admin view."""
    with db_lock:
        try:
            return list(db.products.find({}))
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_all_products_admin: {e}")
            return []

def get_all_transactions_admin():
    """Retrieves all transactions for admin view."""
    with db_lock:
        try:
            return list(db.transactions.find({}).sort('created_at', -1))
        except OperationFailure as e:
            logging.error(f"MongoDB error in get_all_transactions_admin: {e}")
            return []

# --- End of MongoDB Database Functions ---

# (باقي كود البوت كما هو)
# ... (هنا يأتي باقي كود البوت الذي لم يتغير)
# ...

# --- Handlers (مثال على دالة البداية) ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.chat.id, message.chat.username, message.chat.first_name, message.chat.last_name)
    bot.reply_to(message, "مرحباً بك في البوت! يمكنك استخدام /products لرؤية المنتجات.")

# ... (باقي الـ Handlers)
# ...

# --- Main Execution ---
if __name__ == '__main__':
    # --- Dummy Web Server for Render/Heroku Compatibility ---
    app = Flask(__name__)
    
    @app.route('/', methods=['GET', 'HEAD'])
    def index():
        return 'Bot is running (Polling mode, Dummy Server Active)', 200

    # Run the Flask server in a separate thread
    def run_flask():
        port = int(os.environ.get('PORT', 8080))
        logging.info(f"Starting dummy web server on port {port}...")
        # Use debug=False for production
        app.run(host='0.0.0.0', port=port, debug=False)

    # Start polling in a separate thread
    logging.info("Starting Telegram Bot Polling in a separate thread...")
    polling_thread = threading.Thread(target=bot.infinity_polling, daemon=True)
    polling_thread.start()
    
    # Start the dummy server in the main thread
    run_flask()
