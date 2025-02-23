import subprocess
import tempfile
import os
import ast
import time
import resource
import jedi
from typing import Dict, List, Any
from extensions import socketio

# Хранилище ожидающих вводов от пользователя
pending_inputs = {}
# Словарь для отслеживания выполняющихся процессов по идентификатору сессии
running_processes = {}
# Словарь для пометки отмены выполнения для конкретных сессий (if canceled_executions[sid] == True, прекращается вывод)
canceled_executions = {}

class IntellisenseProvider:
    # Белый список модулей для автодополнения
    ALLOWED_MODULES = {
        'math', 'random', 'datetime', 'collections',
        'itertools', 'functools', 'string', 'json',
        'typing', 're', 'time', 'array', 'statistics'
    }

    @classmethod
    def filter_completion(cls, completion) -> bool:
        """Фильтруем автодополнения с потенциально небезопасными именами или ключевыми словами"""
        if any(keyword in completion.name.lower() for keyword in ['/', '\\', 'path', 'system', 'os', 'sys']):
            return False
        # Для модулей разрешаем только из белого списка
        if completion.type == 'module':
            module_name = completion.name.split('.')[0]
            return module_name in cls.ALLOWED_MODULES
        if completion.name.startswith('_'):
            return False
        return True

    @classmethod
    def get_suggestions(cls, text: str, position: Dict[str, int]) -> List[Dict[str, Any]]:
        """Генерируем предложения автодополнения, основываясь на текущей позиции курсора и контексте кода"""
        try:
            lines = text.split("\n")
            line_num = position["lineNumber"] - 1  # Приводим номер строки к индексации с нуля
            column = position["column"]

            if line_num < 0 or line_num >= len(lines):
                return []
            
            # Ограничиваем количество символов в строке
            column = max(0, min(column, len(lines[line_num])))
            current_line = lines[line_num][:column]

            # Если находим контекст импорта, возвращаем предложения из белого списка модулей
            if 'import' in current_line or 'from' in current_line:
                return [
                    {
                        'label': module,
                        'kind': 1,
                        'detail': 'module',
                        'documentation': f'Safe module: {module}',
                        'insertText': module
                    }
                    for module in cls.ALLOWED_MODULES
                    if module.startswith(current_line.split()[-1])
                ]

            # Используем Jedi для получения автодополнений
            script = jedi.Script(code=text, path='<memory>')
            completions = script.complete(line=line_num + 1, column=column)
            
            suggestions = []
            for c in completions:
                # Пропускаем непрошедшие фильтрацию варианты
                if not cls.filter_completion(c):
                    continue
                    
                try:
                    doc = c.docstring(raw=True) or ''
                except Exception as e:
                    print(f"Error getting docstring for {c.name}: {e}")
                    doc = ""

                suggestion = {
                    'label': c.name,
                    'kind': 1,
                    'detail': c.type,
                    'documentation': doc,
                    'insertText': c.name
                }
                suggestions.append(suggestion)

            return suggestions

        except Exception as e:
            print(f"Jedi completion error: {str(e)}")
            return []

def preexec_function():
    """Функция, выполняемая перед запуском процесса, для установки ограничений и группировки процессов"""
    os.setsid()
    mem_limit = 256 * 1024 * 1024  # Ограничение по памяти: 256 МБ
    resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
    cpu_limit = 5  # Ограничение по CPU: 5 секунд
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))

def check_forbidden_operations(code: str) -> bool:
    """Анализируем AST кода, чтобы убедиться в отсутствии запрещенных модулей и вызовов"""
    FORBIDDEN_MODULES = {
        'os', 'sys', 'subprocess', 'shutil', 'pathlib', 
        'socket', 'requests', 'urllib', 'ftplib'
    }
    FORBIDDEN_CALLS = {
        'eval', 'exec', 'open', '__import__', 
        'globals', 'locals', 'compile'
    }
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    module_name = name.name.split('.')[0]
                    if module_name in FORBIDDEN_MODULES:
                        return False
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in FORBIDDEN_MODULES:
                    return False
                for name in node.names:
                    if name.name in FORBIDDEN_CALLS:
                        return False
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in FORBIDDEN_CALLS:
                        return False
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in FORBIDDEN_CALLS:
                        return False
        return True
    except SyntaxError:
        return False

def execute_code(code: str, sid: str):
    """Основная функция для выполнения кода в отдельном процессе и передачи результата через SocketIO"""
    if not check_forbidden_operations(code):
        socketio.emit('execution_output', {'error': "Security Error: Forbidden operations detected"}, room=sid)
        return

    # Создаем временный файл с кодом, в который также внедряется функция custom_input
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as temp_file:
        modified_code = (
            "import sys\n"
            "import builtins\n"
            "def custom_input(prompt=\"\"):\n"
            "    sys.stdout.write(prompt)\n"
            "    sys.stdout.flush()\n"
            "    print(\"<<<INPUT_REQUEST>>>\", flush=True)\n"
            "    return sys.stdin.readline().rstrip(\"\\n\")\n"
            "builtins.input = custom_input\n"
        ) + code
        temp_file.write(modified_code)
        temp_file_path = temp_file.name

    try:
        start_time = time.time()  # Фиксируем время начала выполнения для расчета длительности
        process = subprocess.Popen(
            ['python3', '-u', temp_file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            preexec_fn=preexec_function  # Устанавливаем ограничения и создаем группу процессов
        )
        running_processes[sid] = process  # Отмечаем процесс по идентификатору сессии

        # Читаем stdout построчно до конца или до сигнала отмены
        while True:
            # Проверяем флаг отмены выполнения для данной сессии
            if canceled_executions.get(sid):
                break

            line = process.stdout.readline()
            if line:
                if "<<<INPUT_REQUEST>>>" in line:
                    # Если обнаружен запрос на ввод данных, отправляем запрос клиенту
                    prompt = line.replace("<<<INPUT_REQUEST>>>", "")
                    socketio.emit('execution_input_request', {'prompt': prompt}, room=sid)
                    # Ожидаем, пока клиент не ответит или не произойдет отмена
                    while sid not in pending_inputs and not canceled_executions.get(sid):
                        time.sleep(0.1)
                    if canceled_executions.get(sid):
                        break
                    user_input = pending_inputs.pop(sid)
                    process.stdin.write(user_input + "\n")
                    process.stdin.flush()
                else:
                    # Отправляем строку вывода клиенту
                    socketio.emit('execution_output', {'output': line}, room=sid)
            # Если процесс завершился, читаем оставшийся вывод и выходим из цикла
            if process.poll() is not None:
                remaining = process.stdout.read()
                if remaining:
                    socketio.emit('execution_output', {'output': remaining}, room=sid)
                break
            time.sleep(0.05)

        # Считываем ошибки из stderr после завершения процесса
        error_output = process.stderr.read()
        process.wait()
        execution_time = time.time() - start_time  # Вычисляем время выполнения

        if not canceled_executions.get(sid):
            if error_output:
                socketio.emit('execution_output', {'error': error_output}, room=sid)
            else:
                socketio.emit('execution_output', {
                    'execution_time': f"{execution_time:.2f}s",
                    'status': 'success'
                }, room=sid)
    except Exception as e:
        # При возникновении ошибки отправляем сообщение об ошибке клиенту
        socketio.emit('execution_output', {'error': f"Execution error: {str(e)}"}, room=sid)
    finally:
        # Очистка: удаляем информацию о процессе для данной сессии и удаляем временный файл
        if sid in running_processes:
            del running_processes[sid]
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)