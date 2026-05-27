from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit, join_room
import sqlite3
import hashlib
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")

DB_PATH = "nim_game.db"
K_FACTOR = 32
INITIAL_ELO = 1000

# BDD

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              elo INTEGER DEFAULT 1000,
              wins INTEGER DEFAULT 0,
              losses INTEGER DEFAULT 0,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS games (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              player1 TEXT NOT NULL,
              player2 TEXT NOT NULL,
              winner TEXT,
              elo_change INTEGER,
              played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
              )''')
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ELO

def calculate_elo(winner_elo, loser_elo):
    expected = 1/(1+ 10 **((loser_elo - winner_elo)/400))
    change = round(K_FACTOR*(1-expected))
    return winner_elo + change, loser_elo - change, change

def update_elo(winner, loser):
    conn = get_db()
    w = conn.execute('SELECT elo FROM users WHERE username=?', (winner,)).fetchone()
    l = conn.execute('SELECT elo FROM users WHERE username=?', (loser,)).fetchone()
    if w and l:
        new_w, new_l, change = calculate_elo(w['elo'], l['elo'])
        conn.execute('UPDATE users SET elo=?, wins=wins+1 WHERE username=?', (new_w, winner))
        conn.execute('UPDATE users SET elo=?, losses=losses+1 WHERE username=?', (new_l, loser))
        conn.execute('INSERT INTO games (player1, player2, winner, elo_change) VALUES (?,?,?,?)', (winner, loser, winner, change))
        conn.commit()
        conn.close()
        return change
    conn.close()
    return 0

# GAME

class NimGame:
    def __init__(self, p1, p2, room_id):
        self.board = [1,3,5,7]
        self.player1 = p1
        self.player2 = p2
        self.current = p1
        self.room_id = room_id
        self.winner = None
        self.finished = False

    def move(self, username, row, count):
        if self.finished:
            return False, "Partie terminée"
        if username != self.current:
            return False, "Ce n'est pas votre tour"
        if row <0 or row >=4 or count <1 or count > self.board[row] or self.board[row] ==0:
            return False, "Coup invalide"
        self.board[row] -= count
        if sum(self.board) == 0:
            self.winner = self.player2 if username == self.player1 else self.player1
            self.finished = True
        else :
            self.current = self.player2 if self.current == self.player1 else self.player1
        return True, "OK"
    
    def to_dict(self):
        return{
            'board' : self.board,
            'player1' : self.player1,
            'player2' : self.player2,
            'current' : self.current,
            'winner' : self.winner,
            'finished' : self.finished,
            'room_id' : self.room_id
        }

# ETAT EN MEMOIRE

online_users = {}
active_games = {}
pending_challenges = {}
matchmaking_queue = []

def get_lobby_data():
    conn = get_db()
    lb=[dict(r) for r in conn.execute(
        'SELECT username, elo, wins, losses FROM users ORDER BY elo DESC LIMIT 20'
    ).fetchall()]
    conn.close()
    return{
        'online' : list(set(online_users.values())),
        'leaderboard' : lb,
        'queue' : matchmaking_queue[:]
    }

def find_sid(username):
    for sid, u in online_users.items():
        if u == username:
            return sid
    return None

def start_game(p1,p2):
    room_id = f"game_{p1}_{p2}_{int(datetime.now().timestamp())}"
    active_games[room_id] = NimGame(p1,p2, room_id)
    for player in [p1, p2]:
        sid = find_sid(player)
        if sid:
            socketio.emit('game_start', {'room_id' :room_id}, to=sid)

# ROUTES HTTP

@app.route('/')
def index():
    return redirect(url_for('lobby') if 'username' in session else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method =='POST':
        u = request.form['username'].strip()
        p = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (u,)).fetchone()
        conn.close()
        if user and user['password_hash']== hash_password(p):
            session['username'] = u
            return redirect(url_for('lobby'))
        return render_template('login.html', error= "Identifiants incorrects")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form['username'].strip()
        p = request.form['password']
        if len(u)<3:
            return render_template('register.html', error="Pseudo trop court (min 3 caractères)")
        if len(p)<4:
            return render_template('register.html', error="Mot de passe trop court (min 4 caractères)")
        try:
            conn = get_db()
            conn.execute('INSERT INTO users (username, password_hash) VALUES (?,?)', (u, hash_password(p)))
            conn.commit()
            conn.close()
            session['username'] = u
            return redirect(url_for('lobby'))
        except sqlite3.IntegrityError:
            return render_template('register.html', error="Ce pseudo est déjà pris")
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/lobby')
def lobby():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('lobby.html', username = session['username'])

@app.route('/game/<room_id>')
def game(room_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    if room_id not in active_games:
        return redirect(url_for('lobby'))
    g = active_games[room_id]
    if session['username'] not in [g.player1, g.player2]:
        return redirect(url_for('lobby'))
    return render_template('game.html', username=session['username'], room_id=room_id)

# SOCKET EVENT

@socketio.on('connect')
def on_connect():
    if 'username' in session:
        online_users[request.sid] = session['username']
        socketio.emit('lobby_update', get_lobby_data())

@socketio.on('disconnect')
def on_disconnect():
    username = online_users.pop(request.sid, None)
    if username and username in matchmaking_queue:
        matchmaking_queue.remove(username)
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('challenge')
def on_challenge(data):
    challenger = session.get('username')
    challenged = data.get('username')
    if not challenger or challenger == challenged:
        return
    if challenged not in online_users.values():
        emit('flash', {'type':'error', 'msg' : f"{challenged} n'est plus en ligne"})
        return
    pending_challenges[challenger] = challenged
    sid = find_sid(challenged)
    if sid:
        socketio.emit('challenge_received', {'from' : challenger}, to=sid)

@socketio.on('accept_challenge')
def on_accept_challenge(data):
    challenged = session.get('username')
    challenger = data.get('username')
    if pending_challenges.get(challenger) != challenged:
        return
    del pending_challenges[challenger]
    start_game(challenger, challenged)

@socketio.on('decline_challenge')
def on_decline_challenge(data):
    challenged = session.get('username')
    challenger = data.get('from')
    if pending_challenges.get(challenger) == challenged:
        del pending_challenges[challenger]
        sid = find_sid(challenger)
        if sid:
            socketio.emit('flash', {'type': 'error', 'msg' : f"{challenged} a refusé le défi"}, to=sid)

@socketio.on('join_queue')
def on_join_queue():
    username = session.get('username')
    if not username or username in matchmaking_queue:
        return
    matchmaking_queue.append(username)
    if len(matchmaking_queue)>=2:
        p1,p2 = matchmaking_queue.pop(0), matchmaking_queue.pop(0)
        start_game(p1,p2)
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('leave_queue')
def on_leave_queue():
    username = session.get('username')
    if username in matchmaking_queue:
        matchmaking_queue.remove(username)
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('join_game')
def on_join_game(data):
    room_id = data.get('room_id')
    username = session.get('username')
    if room_id in active_games:
        g = active_games[room_id]
        if username in [g.player1, g.player2]:
            join_room(room_id)
            emit('game_state', g.to_dict())

@socketio.on('make_move')
def on_make_move(data):
    room_id = data.get('room_id')
    username = session.get('username')
    if room_id not in active_games:
        return
    g = active_games[room_id]
    ok, msg = g.move(username, data.get('row'), data.get('count'))
    if ok:
        state = g.to_dict()
        if g.finished:
            loser=g.player2 if g.winner ==g.player1 else g.player1
            elo_change = update_elo(g.winner, loser)
            state['elo_change'] = elo_change
            del active_games[room_id]
            socketio.emit('lobby_update', get_lobby_data())
        socketio.emit('game_state', state, to=room_id)
    else:
        emit('flash', {'type': 'error', 'msg' : msg})

#-------

if __name__ == '__main__':
    init_db()
    # import socket
    # hostname = socket.gethostname()
    # local_ip = socket.gethostbyname(hostname)
    # print(f"\n Game - Serveur lancé !")
    # print(f"\n local : http://localhost:5000")
    # print(f"\n Réseau : http://{local_ip}:5000")
    # print(f"\n Partagez l'adresse suivante à vos adversaires \n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
    # socketio.run(app)

    

    
