import os
import sys
import json
import paramiko
import yaml
from flask import Flask, render_template, request, jsonify, session, send_file, redirect
from werkzeug.utils import secure_filename
import threading
import time
import tempfile
import logging
from io import StringIO

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('logs', exist_ok=True)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# SSH连接管理
class SSHManager:
    def __init__(self):
        self.connections = {}
        self.upload_progress = {}
        self.download_progress = {}
        self.log_monitors = {}  # 添加日志监控器存储

    def get_connection(self, session_id):
        return self.connections.get(session_id)

    def set_connection(self, session_id, ssh_client):
        self.connections[session_id] = ssh_client

    def remove_connection(self, session_id):
        if session_id in self.connections:
            try:
                self.connections[session_id].close()
            except:
                pass
            del self.connections[session_id]

    def set_upload_progress(self, session_id, progress):
        self.upload_progress[session_id] = progress

    def get_upload_progress(self, session_id):
        return self.upload_progress.get(session_id, 0)

    def set_download_progress(self, session_id, progress):
        self.download_progress[session_id] = progress

    def get_download_progress(self, session_id):
        return self.download_progress.get(session_id, 0)

    def set_log_monitor(self, session_id, monitor):
        self.log_monitors[session_id] = monitor

    def get_log_monitor(self, session_id):
        return self.log_monitors.get(session_id)

    def remove_log_monitor(self, session_id):
        if session_id in self.log_monitors:
            del self.log_monitors[session_id]


ssh_manager = SSHManager()

# 固定目录配置
TARGET_DIRECTORY = "/mnt/mnt_data/dzj_dirs/mnt/mnt_data/dzj_dirs/test"
CHROMOSOMES = ["Chr1A", "Chr1B", "Chr1D", "Chr2A", "Chr2B", "Chr2D",
               "Chr3A", "Chr3B", "Chr3D", "Chr4A", "Chr4B", "Chr4D",
               "Chr5A", "Chr5B", "Chr5D", "Chr6A", "Chr6B", "Chr6D",
               "Chr7A", "Chr7B", "Chr7D"]


# 文件上传线程
class FileUploadThread(threading.Thread):
    def __init__(self, ssh_client, local_path, remote_directory, session_id):
        super().__init__()
        self.ssh_client = ssh_client
        self.local_path = local_path
        self.remote_directory = remote_directory
        self.session_id = session_id
        self.daemon = True

    def run(self):
        try:
            # 创建SFTP客户端
            sftp = self.ssh_client.open_sftp()

            # 确保远程目录存在
            try:
                sftp.stat(self.remote_directory)
            except FileNotFoundError:
                # 创建目录
                self.create_remote_directory(sftp, self.remote_directory)

            # 获取文件名
            filename = os.path.basename(self.local_path)
            remote_path = os.path.join(self.remote_directory, filename).replace('\\', '/')

            # 上传文件
            file_size = os.path.getsize(self.local_path)
            uploaded = 0

            with open(self.local_path, 'rb') as local_file:
                with sftp.open(remote_path, 'wb') as remote_file:
                    while True:
                        chunk = local_file.read(32768)  # 32KB chunks
                        if not chunk:
                            break
                        remote_file.write(chunk)
                        uploaded += len(chunk)
                        progress = int((uploaded / file_size) * 100)
                        ssh_manager.set_upload_progress(self.session_id, progress)

            sftp.close()

            # 上传完成，设置进度为100
            ssh_manager.set_upload_progress(self.session_id, 100)

        except Exception as e:
            logger.error(f"文件上传失败: {str(e)}")
            ssh_manager.set_upload_progress(self.session_id, -1)  # 错误状态

    def create_remote_directory(self, sftp, remote_path):
        """递归创建远程目录"""
        directories = remote_path.split('/')
        current_path = ""
        for directory in directories:
            if not directory:
                continue
            current_path += '/' + directory
            try:
                sftp.stat(current_path)
            except FileNotFoundError:
                sftp.mkdir(current_path)


# 文件下载线程
class FileDownloadThread(threading.Thread):
    def __init__(self, ssh_client, remote_path, local_directory, session_id):
        super().__init__()
        self.ssh_client = ssh_client
        self.remote_path = remote_path
        self.local_directory = local_directory
        self.session_id = session_id
        self.daemon = True

    def run(self):
        try:
            # 创建SFTP客户端
            sftp = self.ssh_client.open_sftp()

            # 获取远程文件信息
            remote_file_stat = sftp.stat(self.remote_path)
            file_size = remote_file_stat.st_size

            # 确保本地目录存在
            if not os.path.exists(self.local_directory):
                os.makedirs(self.local_directory)

            # 本地文件路径
            filename = os.path.basename(self.remote_path)
            local_path = os.path.join(self.local_directory, filename)

            # 下载文件
            downloaded = 0
            with sftp.open(self.remote_path, 'rb') as remote_file:
                with open(local_path, 'wb') as local_file:
                    while True:
                        chunk = remote_file.read(32768)  # 32KB chunks
                        if not chunk:
                            break
                        local_file.write(chunk)
                        downloaded += len(chunk)
                        progress = int((downloaded / file_size) * 100)
                        ssh_manager.set_download_progress(self.session_id, progress)

            sftp.close()

            # 下载完成，设置进度为100
            ssh_manager.set_download_progress(self.session_id, 100)

        except Exception as e:
            logger.error(f"文件下载失败: {str(e)}")
            ssh_manager.set_download_progress(self.session_id, -1)  # 错误状态


# 实时日志监控线程 - 按照PyQt5逻辑修改
class RealTimeLogMonitor(threading.Thread):
    def __init__(self, ssh_client, command, session_id):
        super().__init__()
        self.ssh_client = ssh_client
        self.command = command
        self.session_id = session_id
        self.is_running = True
        self.daemon = True

    def run(self):
        try:
            # 创建日志文件
            log_file = f"logs/{self.session_id}_detection.log"
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(f"开始执行命令: {self.command}\n\n")

            # 执行命令并实时获取输出
            stdin, stdout, stderr = self.ssh_client.exec_command(self.command, get_pty=True)

            # 设置非阻塞模式
            import select
            channel = stdout.channel

            # 实时读取输出
            while self.is_running:
                # 检查命令是否完成
                if channel.exit_status_ready():
                    break

                # 读取标准输出
                while channel.recv_ready():
                    output = channel.recv(1024).decode('utf-8', errors='ignore')
                    if output:
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(output)

                # 读取标准错误
                while channel.recv_stderr_ready():
                    error = channel.recv_stderr(1024).decode('utf-8', errors='ignore')
                    if error:
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(f"错误: {error}")

                # 短暂休眠
                time.sleep(0.1)

            # 获取退出状态
            exit_status = channel.recv_exit_status()

            with open(log_file, 'a', encoding='utf-8') as f:
                if exit_status == 0:
                    f.write("\n命令执行成功")
                else:
                    f.write(f"\n命令执行失败，退出码: {exit_status}")

        except Exception as e:
            log_file = f"logs/{self.session_id}_detection.log"
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"执行过程中发生错误: {str(e)}")


@app.route('/')
def index():
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login():
    data = request.json
    hostname = data.get('hostname')
    port = int(data.get('port', 22))
    username = data.get('username')
    password = data.get('password')

    try:
        # 创建SSH连接
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(hostname, port, username, password, timeout=10)

        # 验证并切换到目标目录
        try:
            sftp = ssh_client.open_sftp()
            try:
                sftp.stat(TARGET_DIRECTORY)
            except FileNotFoundError:
                # 创建目录
                stdin, stdout, stderr = ssh_client.exec_command(f"mkdir -p {TARGET_DIRECTORY}")
                if stderr.read():
                    raise Exception(f"创建目录失败: {stderr.read().decode()}")
            sftp.close()

            # 生成会话ID
            session_id = os.urandom(16).hex()
            session['session_id'] = session_id
            session['connection_info'] = {
                'hostname': hostname,
                'port': port,
                'username': username
            }
            ssh_manager.set_connection(session_id, ssh_client)

            return jsonify({
                'success': True,
                'message': f'连接成功! 工作目录: {TARGET_DIRECTORY}',
                'session_id': session_id
            })

        except Exception as e:
            ssh_client.close()
            return jsonify({'success': False, 'message': f'目录操作失败: {str(e)}'})

    except Exception as e:
        return jsonify({'success': False, 'message': f'连接失败: {str(e)}'})


@app.route('/main')
def main_interface():
    if 'session_id' not in session:
        return redirect('/')
    return render_template('main.html', chromosomes=CHROMOSOMES)


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '没有选择文件'})

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '没有选择文件'})

    try:
        # 保存到临时文件
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(temp_path)

        # 启动上传线程
        upload_thread = FileUploadThread(ssh_client, temp_path, TARGET_DIRECTORY, session_id)
        upload_thread.start()

        return jsonify({
            'success': True,
            'message': f'开始上传文件: {filename}',
            'filename': filename
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'上传失败: {str(e)}'})


@app.route('/upload_progress')
def upload_progress():
    if 'session_id' not in session:
        return jsonify({'progress': 0})

    session_id = session['session_id']
    progress = ssh_manager.get_upload_progress(session_id)
    return jsonify({'progress': progress})


@app.route('/download_progress')
def download_progress():
    if 'session_id' not in session:
        return jsonify({'progress': 0})

    session_id = session['session_id']
    progress = ssh_manager.get_download_progress(session_id)
    return jsonify({'progress': progress})


@app.route('/browse_files')
def browse_files():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    file_type = request.args.get('file_type', 'all')

    try:
        # 确保目录存在
        stdin, stdout, stderr = ssh_client.exec_command(f"mkdir -p {TARGET_DIRECTORY}")
        stderr.read()

        # 执行ls命令获取文件列表
        stdin, stdout, stderr = ssh_client.exec_command(f"ls -la {TARGET_DIRECTORY}")
        files = stdout.read().decode().split('\n')

        file_list = []
        for file in files[3:]:  # 跳过前3行
            if file.strip():
                parts = file.split()
                if len(parts) >= 9:
                    filename = ' '.join(parts[8:])
                    if filename not in ['.', '..']:
                        if filter_file(filename, file_type):
                            file_list.append({
                                'name': filename,
                                'is_directory': parts[0].startswith('d'),
                                'full_path': f"{TARGET_DIRECTORY}/{filename}"
                            })

        return jsonify({'success': True, 'files': file_list})

    except Exception as e:
        return jsonify({'success': False, 'message': f'获取文件列表失败: {str(e)}'})


def filter_file(filename, file_type):
    """根据文件类型过滤 - 按照要求修改，FA文件索引路径必须是.fai格式"""
    if file_type == "all":
        return True
    elif file_type == "bam":
        return filename.endswith('.bam')
    elif file_type == "bai":
        # 支持.bai和.csi格式
        return filename.endswith('.bai') or filename.endswith('.csi')
    elif file_type == "fa":
        # 只支持.fai格式
        return filename.endswith('.fai')
    elif file_type == "model":
        return filename.endswith('.pth') or filename.endswith('.pt') or filename.endswith('.h5') or filename.endswith('.model')
    return True


@app.route('/browse_model_files')
def browse_model_files():
    """专门浏览模型文件 - 按照PyQt5逻辑修改"""
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    try:
        # 检查目录是否存在
        stdin, stdout, stderr = ssh_client.exec_command(f"ls {TARGET_DIRECTORY}")
        error_output = stderr.read().decode()

        if error_output:
            return jsonify({'success': False, 'message': f'目录不存在或无法访问: {error_output}'})

        # 获取.pt文件列表
        stdin, stdout, stderr = ssh_client.exec_command(f"find {TARGET_DIRECTORY} -name '*.pt' -type f")
        files = stdout.read().decode().split('\n')

        model_files = []
        for file in files:
            if file.strip():
                filename = os.path.basename(file)
                model_files.append({
                    'name': filename,
                    'full_path': file
                })

        return jsonify({'success': True, 'files': model_files})

    except Exception as e:
        return jsonify({'success': False, 'message': f'获取模型文件列表失败: {str(e)}'})


@app.route('/generate_config', methods=['POST'])
def generate_config():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    data = request.json
    try:
        # 验证FA文件索引路径必须是.fai格式
        fa_path = data.get('fa_path', '')
        if not fa_path.endswith('.fai'):
            return jsonify({'success': False, 'message': 'FA文件索引路径必须是.fai格式!'})

        # 获取选择的染色体
        selected_chromosomes = data.get('chromosomes', [])
        if not selected_chromosomes:
            return jsonify({'success': False, 'message': '请至少选择一个染色体!'})

        # 按照PyQt5逻辑生成data.yaml
        data_config = generate_data_yaml_content(data, selected_chromosomes)

        # 按照PyQt5逻辑生成model.yaml
        model_config = generate_model_yaml_content(data)

        # 写入服务器文件
        write_file_to_server(ssh_client, f"{TARGET_DIRECTORY}/data.yaml", data_config)
        write_file_to_server(ssh_client, f"{TARGET_DIRECTORY}/model.yaml", model_config)

        return jsonify({'success': True, 'message': '配置文件生成成功!'})

    except Exception as e:
        return jsonify({'success': False, 'message': f'生成配置文件失败: {str(e)}'})


def generate_data_yaml_content(data, selected_chromosomes):
    """生成符合PyQt5要求的data.yaml内容 - 确保有正确的换行"""
    data_config = f'''#### REQUIRED ####
bam: "{data['bam_path']}"
fai: "{data['fa_path']}"
n_cpus: {data['cpu_cores']}
class_set: "{data['sv_type']}"
chr_names: {json.dumps(selected_chromosomes)}  # 使用JSON格式确保正确的列表格式
bam_type: "{data['bam_type']}"
signal_set: "SHORT"
signal_set_origin: "SHORT"
logging_level: "INFO"'''

    return data_config


def generate_model_yaml_content(data):
    """生成符合PyQt5要求的model.yaml内容 - 确保有正确的换行"""
    model_config = f'''model_path: "{data['model_path']}"
n_cpus: {data['cpu_cores']}
class_set: "{data['sv_type']}"
bam_type: "{data['bam_type']}"
signal_set: "SHORT"
signal_set_origin: "SHORT"'''

    return model_config


def write_file_to_server(ssh_client, remote_path, content):
    """将内容写入服务器文件"""
    sftp = ssh_client.open_sftp()

    # 确保目录存在
    remote_dir = os.path.dirname(remote_path)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        # 递归创建目录
        create_remote_directory(sftp, remote_dir)

    # 写入文件
    with sftp.open(remote_path, 'w') as remote_file:
        remote_file.write(content)

    sftp.close()


def create_remote_directory(sftp, remote_path):
    """递归创建远程目录"""
    directories = remote_path.split('/')
    current_path = ""
    for directory in directories:
        if not directory:
            continue
        current_path += '/' + directory
        try:
            sftp.stat(current_path)
        except FileNotFoundError:
            sftp.mkdir(current_path)


@app.route('/view_config')
def view_config():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    try:
        # 读取data.yaml
        stdin, stdout, stderr = ssh_client.exec_command(f"cat {TARGET_DIRECTORY}/data.yaml")
        data_content = stdout.read().decode()
        error = stderr.read().decode()

        if error and "No such file" in error:
            return jsonify({'success': False, 'message': 'data.yaml文件不存在，请先生成配置文件'})

        # 读取model.yaml
        stdin, stdout, stderr = ssh_client.exec_command(f"cat {TARGET_DIRECTORY}/model.yaml")
        model_content = stdout.read().decode()
        error = stderr.read().decode()

        if error and "No such file" in error:
            return jsonify({'success': False, 'message': 'model.yaml文件不存在，请先生成配置文件'})

        return jsonify({
            'success': True,
            'data_config': data_content,
            'model_config': model_content
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'读取配置文件失败: {str(e)}'})


@app.route('/start_detection', methods=['POST'])
def start_detection():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    try:
        # 检查配置文件是否存在
        data_yaml_path = f"{TARGET_DIRECTORY}/data.yaml"
        model_yaml_path = f"{TARGET_DIRECTORY}/model.yaml"

        stdin, stdout, stderr = ssh_client.exec_command(f"test -f {data_yaml_path} && echo 'exists'")
        data_exists = stdout.read().decode().strip() == 'exists'

        stdin, stdout, stderr = ssh_client.exec_command(f"test -f {model_yaml_path} && echo 'exists'")
        model_exists = stdout.read().decode().strip() == 'exists'

        if not data_exists or not model_exists:
            return jsonify({'success': False, 'message': '配置文件不存在，请先生成配置文件'})

        # 准备执行命令 - 按照PyQt5逻辑
        conda_path = "/mnt/mnt_data/dzj_dirs/mnt/mnt_data/dzj_dirs/miniconda3"

        command = (
            f"source {conda_path}/etc/profile.d/conda.sh && "
            f"conda activate cue && "
            f"cd {TARGET_DIRECTORY} && "
            f"python /mnt/mnt_data/dzj_dirs/mnt/mnt_data/dzj_dirs/cue/engine/call.py "
            f"--data_config {data_yaml_path} --model_config {model_yaml_path}"
        )

        # 启动实时日志监控线程 - 按照PyQt5逻辑
        log_monitor = RealTimeLogMonitor(ssh_client, command, session_id)
        ssh_manager.set_log_monitor(session_id, log_monitor)
        log_monitor.start()

        return jsonify({'success': True, 'message': '检测任务已开始执行'})

    except Exception as e:
        return jsonify({'success': False, 'message': f'启动检测失败: {str(e)}'})


@app.route('/get_detection_log')
def get_detection_log():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    log_file = f"logs/{session_id}_detection.log"

    try:
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8') as f:
                content = f.read()
            return jsonify({'success': True, 'log': content})
        else:
            return jsonify({'success': True, 'log': '暂无日志'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取日志失败: {str(e)}'})


@app.route('/check_detection_status')
def check_detection_status():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    try:
        # 检查是否有正在运行的python进程（检测任务）
        stdin, stdout, stderr = ssh_client.exec_command("pgrep -f 'python.*call.py'")
        pids = stdout.read().decode().strip()

        is_running = bool(pids)

        # 检查报告文件是否存在
        report_path = f"{TARGET_DIRECTORY}/reports/svs.vcf"
        stdin, stdout, stderr = ssh_client.exec_command(f"test -f {report_path} && echo 'exists'")
        report_exists = stdout.read().decode().strip() == 'exists'

        return jsonify({
            'success': True,
            'is_running': is_running,
            'report_exists': report_exists
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'检查状态失败: {str(e)}'})


@app.route('/download_report')
def download_report():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)
    if not ssh_client:
        return jsonify({'success': False, 'message': 'SSH连接已断开'})

    try:
        report_path = f"{TARGET_DIRECTORY}/reports/svs.vcf"

        # 检查报告文件是否存在
        stdin, stdout, stderr = ssh_client.exec_command(f"test -f {report_path} && echo 'exists'")
        result = stdout.read().decode().strip()

        if result != 'exists':
            return jsonify({'success': False, 'message': '检测报告文件不存在'})

        # 下载文件到临时位置
        temp_dir = tempfile.gettempdir()
        local_path = os.path.join(temp_dir, 'svs.vcf')

        # 启动下载线程
        download_thread = FileDownloadThread(ssh_client, report_path, temp_dir, session_id)
        download_thread.start()

        return jsonify({
            'success': True,
            'message': '开始下载报告文件',
            'local_path': local_path
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'下载报告失败: {str(e)}'})


@app.route('/get_downloaded_file')
def get_downloaded_file():
    if 'session_id' not in session:
        return jsonify({'success': False, 'message': '未登录'})

    filename = request.args.get('filename', 'svs.vcf')
    temp_dir = tempfile.gettempdir()
    local_path = os.path.join(temp_dir, filename)

    if os.path.exists(local_path):
        return send_file(local_path, as_attachment=True, download_name=filename)
    else:
        return jsonify({'success': False, 'message': '文件不存在'})


@app.route('/logout')
def logout():
    if 'session_id' in session:
        ssh_manager.remove_connection(session['session_id'])
        ssh_manager.remove_log_monitor(session['session_id'])
        session.pop('session_id', None)
        session.pop('connection_info', None)
    return jsonify({'success': True, 'message': '已退出登录'})


@app.route('/connection_status')
def connection_status():
    if 'session_id' not in session:
        return jsonify({'connected': False})

    session_id = session['session_id']
    ssh_client = ssh_manager.get_connection(session_id)

    if ssh_client:
        # 测试连接是否仍然有效
        try:
            ssh_client.exec_command("pwd", timeout=5)
            return jsonify({'connected': True, 'info': session.get('connection_info', {})})
        except:
            ssh_manager.remove_connection(session_id)
            ssh_manager.remove_log_monitor(session_id)
            session.pop('session_id', None)
            return jsonify({'connected': False})
    else:
        return jsonify({'connected': False})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
