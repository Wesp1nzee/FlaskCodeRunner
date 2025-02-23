from flask import render_template, request, Flask  
from flask_socketio import emit              
from concurrent.futures import ThreadPoolExecutor  
from code_executor import execute_code, IntellisenseProvider, running_processes, pending_inputs, canceled_executions
import os
import signal
import time
from extensions import socketio   

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio.init_app(app)  

executor = ThreadPoolExecutor()

@app.route('/')
def open_ide():
    return render_template('index.html')

@socketio.on('execute')
def handle_execution(data):
    """Обработчик для получения запроса на выполнение кода от клиента"""
    code = data.get('code', '')
    try:
        # Пытаемся скомпилировать полученный код для проверки на синтаксические ошибки
        compile(code, '<string>', 'exec')
    except SyntaxError as e:
        # Если компиляция завершилась с ошибкой синтаксиса, отправляем сообщение об ошибке клиенту
        emit('execution_output', {
            'error': f"Syntax Error: {str(e)}\nLine {e.lineno}, Column {e.offset}\n{e.text}\n{'^':>{e.offset}}"
        })
        return

    # Сбрасываем флаг отмены выполнения для данной сессии перед запуском нового кода
    canceled_executions.pop(request.sid, None)
    # Передаем выполнение кода в пул потоков для асинхронного выполнения
    executor.submit(execute_code, code, request.sid)

@socketio.on('cancel_execution')
def cancel_execution():
    """Обработчик для отмены выполнения кода по запросу клиента"""
    sid = request.sid
    process = running_processes.get(sid)
    if process:
        try:
            # Отправляем сигнал SIGTERM для завершения процесса (всей группы процессов)
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            time.sleep(0.5)  # Даем время для корректного завершения
            # Если процесс все еще работает, отправляем SIGKILL для принудительного завершения
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            # Помечаем, что выполнение для данной сессии отменено
            canceled_executions[sid] = True
        except Exception as e:
            print(f"Error terminating process group: {e}")

        # Отправляем клиенту сообщение об отмене выполнения
        emit('execution_output', {'error': 'Execution cancelled by user.'}, room=sid)
    else:
        emit('execution_output', {'error': 'No running process found.'}, room=sid)

@socketio.on('stdin_input')
def handle_stdin_input(data):
    """Обработчик для передачи пользовательского ввода в выполняемый процесс"""
    sid = request.sid
    process = running_processes.get(sid)
    if process and process.stdin:
        input_value = data.get('text', '')
        try:
            # Передаем ввод в процесс через стандартный ввод
            process.stdin.write(input_value.encode())
            process.stdin.flush()
        except Exception as e:
            # Если произошла ошибка при передаче ввода, отправляем сообщение об ошибке клиенту
            emit('execution_output', {'error': f"Error sending input: {str(e)}"}, room=sid)

@socketio.on('completion')
def handle_completion(data):
    """Обработчик запроса автодополнения кода"""
    text = data.get('text', '')
    position = data.get('position', {})
    
    # Проверка на потенциально небезопасные варианты автодополнения
    if any(keyword in text.lower() for keyword in [
        'import os', 'import sys', 'import subprocess', 
        'eval(', 'exec(', '__import__', 'open('
    ]):
        emit('completion_result', {'suggestions': []})
        return
        
    # Получаем предложения автодополнения от IntellisenseProvider
    suggestions = IntellisenseProvider.get_suggestions(text, position)
    emit('completion_result', {'suggestions': suggestions})

@socketio.on('lint')
def handle_linting(data):
    """Обработчик для проверки синтаксиса (линтинга) кода"""
    code = data.get('code', '')
    try:
        compile(code, '<string>', 'exec')
        emit('lint_result', {'valid': True})
    except SyntaxError as e:
        # Если обнаружена синтаксическая ошибка, отправляем детали ошибки клиенту
        emit('lint_result', {
            'valid': False,
            'error': str(e),
            'line': e.lineno,
            'column': e.offset
        })

@socketio.on('input_response')
def handle_input_response(data):
    """Обработчик для получения ответа на запрос ввода от клиента"""
    sid = data.get('sid') or request.sid
    pending_inputs[sid] = data.get('input', '')

if __name__ == '__main__':
    socketio.run(app, debug=True)