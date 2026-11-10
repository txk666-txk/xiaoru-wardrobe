import os
import json
import codecs
import logging
import requests
from configparser import ConfigParser
from urllib.parse import quote, urlsplit
import functools
from threading import Lock
import threading
from utils import util

LOGGER = logging.getLogger(__name__)

# 条件导入 langsmith
try:
    # 检查是否有相关环境变量或包可用
    langsmith_env_vars = ['LANGCHAIN_API_KEY', 'LANGSMITH_API_KEY', 'LANGCHAIN_TRACING_V2']
    has_langsmith_env = any(os.getenv(var) for var in langsmith_env_vars)
    
    if has_langsmith_env:
        from langsmith.schemas import Feedback
        LOGGER.debug("检测到 LangSmith 环境变量，已导入 langsmith.schemas.Feedback")
    else:
        # 尝试导入以检查包是否可用
        import langsmith.schemas
        from langsmith.schemas import Feedback
        LOGGER.debug("langsmith 包可用，已导入 langsmith.schemas.Feedback")
except ImportError:
    # langsmith 包不可用，定义一个占位符类
    class Feedback:
        """langsmith 不可用时的占位符类"""
        pass
    LOGGER.debug("langsmith 包不可用，使用占位符类。如需使用 LangSmith 功能，请安装: pip install langsmith")
except Exception as e:
    # 其他导入错误
    class Feedback:
        """langsmith 导入失败时的占位符类"""
        pass
    LOGGER.debug(f"langsmith 导入失败: {str(e)}，使用占位符类")

# 线程本地存储，用于支持多个项目配置
_thread_local = threading.local()

# 全局锁，确保线程安全
lock = Lock()
def synchronized(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with lock:
            return func(*args, **kwargs)
    return wrapper

# 默认配置，用于全局访问
config: json = None
system_config: ConfigParser = None
system_chrome_driver = None
key_ali_nls_key_id = None
key_ali_nls_key_secret = None
key_ali_nls_app_key = None
key_ms_tts_key = None
Key_ms_tts_region = None
baidu_emotion_app_id = None
baidu_emotion_api_key = None
baidu_emotion_secret_key = None
key_gpt_api_key = None
gpt_model_engine = None
proxy_config = None
ASR_mode = None
local_asr_ip = None 
local_asr_port = None 
ltp_mode = None
gpt_base_url = None
big_model_engine = None
big_model_base_url = None
big_model_api_key = None
tts_module = None
gptsovits_api_url = None
gptsovits_ref_audio_path = None
gptsovits_prompt_text = None
gptsovits_prompt_lang = None
gptsovits_text_lang = None
gptsovits_timeout = None
key_ali_tss_key_id = None
key_ali_tss_key_secret = None
key_ali_tss_app_key = None
volcano_tts_appid = None
volcano_tts_access_token = None
volcano_tts_cluster = None
volcano_tts_voice_type = None
start_mode = None
fay_url = None
system_conf_path = None
config_json_path = None
use_bionic_memory = None

# Embedding API 配置全局变量
embedding_api_model = None
embedding_api_base_url = None
embedding_api_key = None

SYSTEM_CONFIG_ENV_KEY = 'FAY_SYSTEM_CONF_JSON'

# 避免重复加载配置中心导致日志刷屏
_last_loaded_project_id = None
_last_loaded_config = None
_last_loaded_from_api = False  # 表示上次加载来自配置中心（含缓存）
_last_loaded_center_url = None

# Remote configuration is opt-in. A checkout never connects to an upstream
# community server just because local configuration has not been initialized.
CONFIG_SERVER = {'BASE_URL': '', 'API_KEY': '', 'PROJECT_ID': ''}


def _refresh_config_center():
    CONFIG_SERVER.update({
        'BASE_URL': (os.getenv('FAY_CONFIG_CENTER_URL') or '').strip().rstrip('/'),
        'API_KEY': (os.getenv('FAY_CONFIG_CENTER_API_KEY') or '').strip(),
        'PROJECT_ID': (os.getenv('FAY_CONFIG_CENTER_ID') or '').strip(),
    })


def _config_center_enabled():
    base_url = CONFIG_SERVER['BASE_URL']
    project_id = CONFIG_SERVER['PROJECT_ID']
    if not base_url and not project_id and not CONFIG_SERVER['API_KEY']:
        return False
    if not base_url or not project_id:
        raise ValueError('配置中心需同时设置 FAY_CONFIG_CENTER_URL 和 FAY_CONFIG_CENTER_ID；'
                         '本地启动请移除配置中心变量并运行 scripts/initialize_local_config.ps1。')
    parsed = urlsplit(base_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError('FAY_CONFIG_CENTER_URL 必须是无账号、查询参数和片段的 HTTP(S) 地址。')
    # Accessing port also rejects malformed or out-of-range values before I/O.
    parsed.port
    return True


_refresh_config_center()


def _config_parser_to_dict(parser):
    data = {}
    if parser is None:
        return data
    for section in parser.sections():
        data[section] = {}
        for key, value in parser.items(section):
            data[section][key] = value
    return data


def _dict_to_config_parser(data):
    parser = ConfigParser()
    if not isinstance(data, dict):
        return parser
    for section, items in data.items():
        if not isinstance(items, dict):
            continue
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in items.items():
            parser.set(section, str(key), '' if value is None else str(value))
    return parser


def _save_system_config_to_env(parser, project_id=None, source='local'):
    if parser is None:
        return
    payload = {
        'meta': {
            'project_id': project_id,
            'source': source,
            'center_url': CONFIG_SERVER['BASE_URL'] if source == 'api' else None,
        },
        'sections': _config_parser_to_dict(parser),
    }
    try:
        os.environ[SYSTEM_CONFIG_ENV_KEY] = json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        util.log(2, f"save system.conf to env failed: {str(e)}")


def _load_system_config_from_env(expected_project_id=None, expected_center_url=None):
    raw_value = os.getenv(SYSTEM_CONFIG_ENV_KEY)
    if not raw_value:
        return None

    try:
        payload = json.loads(raw_value)
        meta = {}
        sections = payload
        if isinstance(payload, dict) and isinstance(payload.get('sections'), dict):
            meta = payload.get('meta') or {}
            sections = payload.get('sections') or {}

        env_project_id = meta.get('project_id')
        if expected_project_id and env_project_id != expected_project_id:
            return None
        if expected_center_url and meta.get('center_url') != expected_center_url:
            return None
        if not expected_project_id and meta.get('source') == 'api':
            return None

        parser = _dict_to_config_parser(sections)
        if parser.sections():
            return parser
    except Exception as e:
        util.log(2, f"load system.conf from env failed: {str(e)}")

    return None

def load_config_from_api(project_id=None):
    global CONFIG_SERVER

    """
    从API加载配置
    
    Args:
        project_id: 项目ID，如果为None则使用全局设置的项目ID
    
    Returns:
        包含配置信息的字典，加载失败则返回None
    """
    _refresh_config_center()
    if not _config_center_enabled():
        return None
    pid = project_id or CONFIG_SERVER['PROJECT_ID']
    url = f"{CONFIG_SERVER['BASE_URL']}/api/projects/{quote(str(pid), safe='')}/config"
    headers = {'Content-Type': 'application/json'}
    if CONFIG_SERVER['API_KEY']:
        headers['X-API-Key'] = CONFIG_SERVER['API_KEY']

    try:
        # 发送API请求
        response = requests.get(url, headers=headers, timeout=(3.05, 10), allow_redirects=False)
        
        # 检查响应状态
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                # 提取配置数据
                project_data = result.get('project', {})
                
                # 创建并填充ConfigParser对象
                sys_config = ConfigParser()
                sys_config.add_section('key')
                
                # 获取系统配置字典
                system_dict = project_data.get('system_config', {})
                for section, items in system_dict.items():
                    if not sys_config.has_section(section):
                        sys_config.add_section(section)
                    for key, value in items.items():
                        sys_config.set(section, key, str(value))
                
                # 获取用户配置
                user_config = project_data.get('config_json', {})
                
                # 创建配置字典
                config_dict = {
                    'system_config': sys_config,
                    'config': user_config,
                    'project_id': pid,
                    'name': project_data.get('name', ''),
                    'description': project_data.get('description', ''),
                    'source': 'api'  # 标记配置来源
                }
                
                # 提取所有配置项到配置字典
                for section in sys_config.sections():
                    for key, value in sys_config.items(section):
                        config_dict[f'{section}_{key}'] = value
                
                return config_dict
            else:
                util.log(2, f"API错误: {result.get('message', '未知错误')}")
        else:
            util.log(2, f"API请求失败: HTTP状态码 {response.status_code}")
    except Exception as e:
        util.log(2, f"从API加载配置时出错: {str(e)}")
    
    return None

@synchronized
def load_config(force_reload=False):
    """
    加载本地配置；仅显式配置 URL 和项目 ID 时启用远程配置中心。
    
    Returns:
        包含配置信息的字典
    """
    global config
    global system_config
    global key_ali_nls_key_id
    global key_ali_nls_key_secret
    global key_ali_nls_app_key
    global key_ms_tts_key
    global key_ms_tts_region
    global baidu_emotion_app_id
    global baidu_emotion_secret_key
    global baidu_emotion_api_key
    global key_gpt_api_key
    global gpt_model_engine
    global proxy_config
    global ASR_mode
    global local_asr_ip 
    global local_asr_port
    global ltp_mode 
    global gpt_base_url
    global big_model_engine
    global big_model_base_url
    global big_model_api_key
    global tts_module
    global gptsovits_api_url
    global gptsovits_ref_audio_path
    global gptsovits_prompt_text
    global gptsovits_prompt_lang
    global gptsovits_text_lang
    global gptsovits_timeout
    global key_ali_tss_key_id
    global key_ali_tss_key_secret
    global key_ali_tss_app_key
    global volcano_tts_appid
    global volcano_tts_access_token
    global volcano_tts_cluster
    global volcano_tts_voice_type
    global start_mode
    global fay_url
    global use_bionic_memory
    global embedding_api_model
    global embedding_api_base_url
    global embedding_api_key

    global CONFIG_SERVER
    global system_conf_path
    global config_json_path
    global _last_loaded_project_id
    global _last_loaded_config
    global _last_loaded_from_api
    global _last_loaded_center_url

    _refresh_config_center()
    using_config_center = _config_center_enabled()
    center_id = CONFIG_SERVER['PROJECT_ID'] if using_config_center else None
    center_url = CONFIG_SERVER['BASE_URL'] if using_config_center else None
    same_cached_center = (
        using_config_center and _last_loaded_from_api
        and _last_loaded_project_id == center_id
        and _last_loaded_center_url == center_url
        and _last_loaded_config is not None
    )
    if same_cached_center and not force_reload:
        return _last_loaded_config

    env_system_config = None if force_reload else _load_system_config_from_env(
        expected_project_id=center_id, expected_center_url=center_url,
    )
    default_system_conf_path = os.path.join(os.getcwd(), 'system.conf')
    default_config_json_path = os.path.join(os.getcwd(), 'config.json')
    cache_system_conf_path = os.path.join(os.getcwd(), 'cache_data', 'system.conf')
    cache_config_json_path = os.path.join(os.getcwd(), 'cache_data', 'config.json')

    if using_config_center:
        system_conf_path = cache_system_conf_path
        config_json_path = cache_config_json_path
        util.log(1, '已显式启用配置中心，正在加载项目配置')
        api_config = load_config_from_api(center_id)
        if api_config:
            system_config = api_config['system_config']
            env_system_config = system_config
            _save_system_config_to_env(system_config, project_id=center_id, source='api')
            config = api_config['config']
            save_api_config_to_local(
                api_config, system_conf_path, config_json_path,
                save_config_json=not os.path.exists(config_json_path),
                save_system_conf=False,
            )
        else:
            util.log(2, '配置中心加载失败，尝试使用同一配置中心的缓存')
            if same_cached_center:
                return _last_loaded_config
            if env_system_config is None:
                env_system_config = _load_system_config_from_env(
                    expected_project_id=center_id, expected_center_url=center_url,
                )
    else:
        # Missing local files must not trigger any network request or use a
        # previous remote project's configuration implicitly.
        system_conf_path = default_system_conf_path
        config_json_path = default_config_json_path
        if not os.path.isfile(system_conf_path) or not os.path.isfile(config_json_path):
            raise FileNotFoundError('本地配置不完整，请先运行 scripts/initialize_local_config.ps1，'
                                    '再填写 system.conf 后启动。未启用远程配置中心。')

    sys_conf_exists = env_system_config is not None or os.path.exists(system_conf_path)
    config_json_exists = os.path.exists(config_json_path)
    if not sys_conf_exists or not config_json_exists:
        if using_config_center:
            raise RuntimeError('配置中心不可用且缓存不完整，请检查显式配置的地址、项目 ID 和连接；'
                               '也可移除配置中心变量并运行 scripts/initialize_local_config.ps1 使用本地配置。')
        raise FileNotFoundError('本地配置不完整，请先运行 scripts/initialize_local_config.ps1，'
                                '再填写 system.conf 后启动。未启用远程配置中心。')

    # 如果本地文件存在，从本地文件加载
    # 加载system.conf
    if env_system_config is not None:
        system_config = env_system_config
    else:
        system_config = ConfigParser()
        if os.path.exists(system_conf_path):
            system_config.read(system_conf_path, encoding='UTF-8')
            _save_system_config_to_env(
                system_config,
                project_id=CONFIG_SERVER['PROJECT_ID'] if using_config_center else None,
                source='api' if using_config_center else 'local'
            )

    if not system_config.has_section('key'):
        system_config.add_section('key')
    
    # 从system.conf中读取所有配置项
    key_ali_nls_key_id = system_config.get('key', 'ali_nls_key_id', fallback=None)
    key_ali_nls_key_secret = system_config.get('key', 'ali_nls_key_secret', fallback=None)
    key_ali_nls_app_key = system_config.get('key', 'ali_nls_app_key', fallback=None)
    key_ali_tss_key_id = system_config.get('key', 'ali_tss_key_id', fallback=None)
    key_ali_tss_key_secret = system_config.get('key', 'ali_tss_key_secret', fallback=None)
    key_ali_tss_app_key = system_config.get('key', 'ali_tss_app_key', fallback=None)
    key_ms_tts_key = system_config.get('key', 'ms_tts_key', fallback=None)
    key_ms_tts_region  = system_config.get('key', 'ms_tts_region', fallback=None)
    baidu_emotion_app_id = system_config.get('key', 'baidu_emotion_app_id', fallback=None)
    baidu_emotion_api_key = system_config.get('key', 'baidu_emotion_api_key', fallback=None)
    baidu_emotion_secret_key = system_config.get('key', 'baidu_emotion_secret_key', fallback=None)
    key_gpt_api_key = system_config.get('key', 'gpt_api_key', fallback=None)
    gpt_model_engine = system_config.get('key', 'gpt_model_engine', fallback=None)
    ASR_mode = system_config.get('key', 'ASR_mode', fallback=None)
    local_asr_ip = system_config.get('key', 'local_asr_ip', fallback=None)
    local_asr_port = system_config.get('key', 'local_asr_port', fallback=None)
    proxy_config = system_config.get('key', 'proxy_config', fallback=None)
    ltp_mode = system_config.get('key', 'ltp_mode', fallback=None)
    gpt_base_url = system_config.get('key', 'gpt_base_url', fallback=None)
    big_model_engine = system_config.get('key', 'big_model_engine', fallback=None) or None
    big_model_base_url = system_config.get('key', 'big_model_base_url', fallback=None) or None
    big_model_api_key = system_config.get('key', 'big_model_api_key', fallback=None) or None
    tts_module = system_config.get('key', 'tts_module', fallback=None)
    gptsovits_api_url = system_config.get('key', 'gptsovits_api_url', fallback='http://127.0.0.1:9880/tts')
    gptsovits_ref_audio_path = system_config.get('key', 'gptsovits_ref_audio_path', fallback='')
    gptsovits_prompt_text = system_config.get('key', 'gptsovits_prompt_text', fallback='')
    gptsovits_prompt_lang = system_config.get('key', 'gptsovits_prompt_lang', fallback='zh')
    gptsovits_text_lang = system_config.get('key', 'gptsovits_text_lang', fallback='zh')
    gptsovits_timeout = system_config.get('key', 'gptsovits_timeout', fallback='120')
    volcano_tts_appid = system_config.get('key', 'volcano_tts_appid', fallback=None)
    volcano_tts_access_token = system_config.get('key', 'volcano_tts_access_token', fallback=None)
    volcano_tts_cluster = system_config.get('key', 'volcano_tts_cluster', fallback=None)
    volcano_tts_voice_type = system_config.get('key', 'volcano_tts_voice_type', fallback=None)

    # 读取 Embedding API 配置（base_url/key 可单独配置，未配置则复用 LLM）
    embedding_api_model = system_config.get('key', 'embedding_api_model', fallback='BAAI/bge-large-zh-v1.5')
    _embedding_base_url_cfg = system_config.get('key', 'embedding_base_url', fallback='')
    _embedding_api_key_cfg = system_config.get('key', 'embedding_api_key', fallback='')
    embedding_api_base_url = _embedding_base_url_cfg.strip() or gpt_base_url
    embedding_api_key = _embedding_api_key_cfg.strip() or key_gpt_api_key

    start_mode = system_config.get('key', 'start_mode', fallback=None)
    fay_url = system_config.get('key', 'fay_url', fallback=None)
    # 如果fay_url为空或None，则动态获取本机IP地址
    if not fay_url:
        from utils.util import get_local_ip
        local_ip = get_local_ip()
        fay_url = f"http://{local_ip}:5000"
        # 更新system_config中的值，但不写入文件
        if not system_config.has_section('key'):
            system_config.add_section('key')
        system_config.set('key', 'fay_url', fay_url)
        _save_system_config_to_env(
            system_config,
            project_id=CONFIG_SERVER['PROJECT_ID'] if using_config_center else None,
            source='api' if using_config_center else 'local'
        )
    
    # 读取用户配置
    with codecs.open(config_json_path, encoding='utf-8') as f:
        config = json.load(f)

    # 读取仿生记忆配置
    use_bionic_memory = config.get('memory', {}).get('use_bionic_memory', False)

    # 构建配置字典
    config_dict = {
        'system_config': system_config,
        'config': config,
        'ali_nls_key_id': key_ali_nls_key_id,
        'ali_nls_key_secret': key_ali_nls_key_secret,
        'ali_nls_app_key': key_ali_nls_app_key,
        'ms_tts_key': key_ms_tts_key,
        'ms_tts_region': key_ms_tts_region,
        'baidu_emotion_app_id': baidu_emotion_app_id,
        'baidu_emotion_api_key': baidu_emotion_api_key,
        'baidu_emotion_secret_key': baidu_emotion_secret_key,
        'gpt_api_key': key_gpt_api_key,
        'gpt_model_engine': gpt_model_engine,
        'ASR_mode': ASR_mode,
        'local_asr_ip': local_asr_ip,
        'local_asr_port': local_asr_port,
        'proxy_config': proxy_config,
        'ltp_mode': ltp_mode,

        'gpt_base_url': gpt_base_url,
        'tts_module': tts_module,
        'gptsovits_api_url': gptsovits_api_url,
        'gptsovits_ref_audio_path': gptsovits_ref_audio_path,
        'gptsovits_prompt_text': gptsovits_prompt_text,
        'gptsovits_prompt_lang': gptsovits_prompt_lang,
        'gptsovits_text_lang': gptsovits_text_lang,
        'gptsovits_timeout': gptsovits_timeout,
        'ali_tss_key_id': key_ali_tss_key_id,
        'ali_tss_key_secret': key_ali_tss_key_secret,
        'ali_tss_app_key': key_ali_tss_app_key,
        'volcano_tts_appid': volcano_tts_appid,
        'volcano_tts_access_token': volcano_tts_access_token,
        'volcano_tts_cluster': volcano_tts_cluster,
        'volcano_tts_voice_type': volcano_tts_voice_type,

        'start_mode': start_mode,
        'fay_url': fay_url,
        'use_bionic_memory': use_bionic_memory,

        # Embedding API 配置
        'embedding_api_model': embedding_api_model,
        'embedding_api_base_url': embedding_api_base_url,
        'embedding_api_key': embedding_api_key,

        'source': 'api' if using_config_center else 'local'  # 标记配置来源
    }

    _last_loaded_project_id = CONFIG_SERVER['PROJECT_ID'] if using_config_center else None
    _last_loaded_center_url = center_url
    _last_loaded_config = config_dict
    _last_loaded_from_api = using_config_center
    
    return config_dict

def save_api_config_to_local(api_config, system_conf_path, config_json_path, save_config_json=True, save_system_conf=False):
    """
    Persist API config to local files.
    
    Args:
        api_config: API response dict.
        system_conf_path: Path to system.conf.
        config_json_path: Path to config.json.
        save_config_json: Whether to write config.json.
    """
    try:
    # 确保目录存在.
        if save_system_conf and system_conf_path:
            os.makedirs(os.path.dirname(system_conf_path), exist_ok=True)
        os.makedirs(os.path.dirname(config_json_path), exist_ok=True)
        
        # 始终刷新 system.conf.
        if save_system_conf and system_conf_path:
            with open(system_conf_path, 'w', encoding='utf-8') as f:
                api_config['system_config'].write(f)
        
        # 默认只在首次下载时保存config.json.
        if save_config_json:
            with codecs.open(config_json_path, 'w', encoding='utf-8') as f:
                json.dump(api_config['config'], f, ensure_ascii=False, indent=4)
        if save_system_conf and system_conf_path:
            util.log(1, f"cached config center files to local: {system_conf_path}, {config_json_path}")
            return
        if save_config_json:
            util.log(1, f"cached config center config.json to local: {config_json_path}")
            return
        return
    except Exception as e:
        util.log(2, f"保存配置中心配置到本地文件时出错: {str(e)}")

@synchronized
def save_config(config_data):
    """
    保存配置到config.json文件
    
    Args:
        config_data: 要保存的配置数据
        config_dir: 配置文件目录，如果为None则使用当前目录
    """
    global config
    global config_json_path
    
    config = config_data
    
    # 保存到文件
    with codecs.open(config_json_path, mode='w', encoding='utf-8') as file:
        file.write(json.dumps(config_data, sort_keys=True, indent=4, separators=(',', ': ')))
