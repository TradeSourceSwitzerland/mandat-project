# Complete content from commit 58e7aa24d327f00e3b71eefdd6decb39257decd7 

# Import required libraries
from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy'})

@app.route('/', methods=['GET'])
def root():
    return jsonify({'status': 'healthy'})  # Health check response

if __name__ == '__main__':
    app.run(debug=True)