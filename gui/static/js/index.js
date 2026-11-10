// fayApp.js

// 全局函数：打开图片文件
window.openImageFile = function(encodedPath) {
  const filePath = decodeURIComponent(encodedPath);
  const baseUrl = window.location.protocol + '//' + window.location.hostname + ':' + window.location.port;

  fetch(`${baseUrl}/api/open-image`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ path: filePath })
  })
  .then(response => response.json())
  .then(data => {
    if (!data.success) {
      console.error('打开图片失败:', data.message);
      alert('打开图片失败: ' + data.message);
    }
  })
  .catch(error => {
    console.error('请求失败:', error);
    alert('打开图片时发生错误');
  });
};

class FayInterface {
  constructor(baseWsUrl, baseApiUrl, vueInstance) {
    this.baseWsUrl = baseWsUrl;
    this.baseApiUrl = baseApiUrl;
    this.websocket = null;
    this.reconnectTimer = null;
    this.humanWebsocket = null;
    this.humanReconnectTimer = null;
    this.audioQueue = [];
    this.audioPlayer = new Audio();
    this.currentAudioItem = null;
    this.audioPlaying = false;
    this.audioBlocked = false;
    this.destroyed = false;
    this.vueInstance = vueInstance;

    this.unlockAudioHandler = () => this.unlockAudio();
    // Keep this listener until destroy: the first user gesture often happens
    // before TTS returns, while a later audio item may still need unlocking.
    document.addEventListener('pointerdown', this.unlockAudioHandler);
  }

  async connectHumanWebSocket() {
    if (this.destroyed) {
      return;
    }
    if (this.humanWebsocket) {
      this.humanWebsocket.onopen = null;
      this.humanWebsocket.onmessage = null;
      this.humanWebsocket.onclose = null;
      this.humanWebsocket.onerror = null;
      this.humanWebsocket.close();
    }

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const humanWsUrl = `${wsProtocol}//${window.location.hostname}:10002`;
    const username = this.resolvedUsername();
    this.humanUsername = username;
    const generation = this.humanGeneration = (this.humanGeneration || 0) + 1;
    let ticket;
    try { ticket = await window.faySecurity.ticket(username, 'human'); }
    catch (error) { this.connectionError(error, 'human'); return; }
    if (this.destroyed || generation !== this.humanGeneration) return;
    this.humanWebsocket = new WebSocket(`${humanWsUrl}/?ticket=${encodeURIComponent(ticket)}`);

    this.humanWebsocket.onopen = () => {
      console.log('Digital human audio WebSocket opened');
      this.registerHumanOutput();
      if (window.fayAvatar) {
        window.fayAvatar.handleConnection(true);
      }
    };

    this.humanWebsocket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.Topic === 'human' && message.Data && message.Data.Key === 'audio') {
          this.enqueueAudio(message);
        } else if (window.fayAvatar) {
          window.fayAvatar.handleHumanMessage(message);
        }
      } catch (error) {
        console.error('Invalid digital human audio message:', error);
      }
    };

    this.humanWebsocket.onclose = () => {
      if (window.fayAvatar) {
        window.fayAvatar.handleConnection(false);
      }
      clearTimeout(this.humanReconnectTimer);
      if (!this.destroyed) {
        this.humanReconnectTimer = setTimeout(() => this.connectHumanWebSocket(), 5000);
      }
    };

    this.humanWebsocket.onerror = (error) => {
      console.error('Digital human audio WebSocket error:', error);
      if (window.fayAvatar) {
        window.fayAvatar.handleConnection(false);
      }
    };
  }

  registerHumanOutput(username = null) {
    const resolvedUsername = username || this.resolvedUsername();
    if (this.humanUsername && this.humanUsername !== resolvedUsername) {
      this.audioPlayer.pause();
      this.audioQueue = [];
      this.currentAudioItem = null;
      this.audioPlaying = false;
      this.audioPlayer.onended = null;
    }
    if (this.humanUsername !== resolvedUsername) this.connectHumanWebSocket();
    if (this.webUsername !== resolvedUsername) this.connectWebSocket();
    if (!this.humanWebsocket || this.humanWebsocket.readyState !== WebSocket.OPEN) {
      return;
    }
    if (this.humanUsername !== resolvedUsername) return;
    this.humanWebsocket.send(JSON.stringify({ Username: resolvedUsername, Output: true }));
  }

  resolvedUsername() {
    const user = this.vueInstance.selectedUser;
    return Array.isArray(user) && user.length > 1 ? user[1] : 'User';
  }

  connectionError(error, channel) {
    console.error('聊天连接验证失败:', error);
    if (this.vueInstance.$message) this.vueInstance.$message.error(error.message);
    if (this.destroyed) return;
    if (channel === 'human') {
      clearTimeout(this.humanReconnectTimer);
      this.humanReconnectTimer = setTimeout(() => this.connectHumanWebSocket(), 5000);
    } else {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(() => this.connectWebSocket(), 5000);
    }
  }

  unlockAudio() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass && !this.audioContext) {
      this.audioContext = new AudioContextClass();
    }
    if (this.audioContext && this.audioContext.state === 'suspended') {
      this.audioContext.resume().catch(() => {});
    }
    if (this.audioBlocked) {
      this.audioBlocked = false;
      this.audioPlayer.play().catch(() => {
        this.audioBlocked = true;
      });
    }
  }

  enqueueAudio(message) {
    const data = message && message.Data ? message.Data : null;
    if (!data) {
      return;
    }
    // Fay 已在服务器所在电脑本机播放时，网页不再重复播放同一段音频。
    // 这既避免叠音，也让后端可以准确维护 speaking 状态，防止麦克风收录自身播音。
    if (this.vueInstance && this.vueInstance.play_sound_enabled === true) {
      if (window.fayAvatar) {
        window.fayAvatar.handleHumanMessage(message);
      }
      return;
    }

    if (!data.HttpValue) {
      if (window.fayAvatar) {
        window.fayAvatar.handleHumanMessage(message);
      }
      return;
    }

    const audioUrl = new URL(data.HttpValue, window.location.origin);
    if (audioUrl.hostname === '127.0.0.1' && window.location.hostname !== '127.0.0.1') {
      audioUrl.hostname = window.location.hostname;
    }
    this.audioQueue.push({
      url: audioUrl.toString(),
      message: message,
      avatarStarted: false
    });
    this.playNextAudio();
  }

  playNextAudio() {
    if (this.audioPlaying || this.audioQueue.length === 0) {
      return;
    }

    this.audioPlaying = true;
    const item = this.audioQueue.shift();
    this.currentAudioItem = item;
    this.audioPlayer.src = item.url;
    const startAvatar = () => {
      if (!item.avatarStarted && window.fayAvatar) {
        item.avatarStarted = true;
        window.fayAvatar.handleHumanMessage(item.message);
      }
    };
    this.audioPlayer.onplaying = startAvatar;
    this.audioPlayer.onended = () => {
      startAvatar();
      if (window.fayAvatar) {
        window.fayAvatar.finishActiveSpeech(item.message.Data);
      }
      this.audioPlaying = false;
      this.audioBlocked = false;
      this.currentAudioItem = null;
      this.playNextAudio();
    };
    this.audioPlayer.onerror = () => {
      console.error('Failed to load Fay audio:', this.audioPlayer.src);
      if (item.avatarStarted && window.fayAvatar) {
        window.fayAvatar.finishActiveSpeech(item.message.Data);
      }
      this.audioPlaying = false;
      this.audioBlocked = false;
      this.currentAudioItem = null;
      this.playNextAudio();
    };
    this.audioPlayer.play().catch((error) => {
      console.warn('Browser blocked audio autoplay; click the page once to continue.', error);
      this.audioBlocked = true;
    });
  }

  async connectWebSocket() {
    if (this.destroyed) {
      return;
    }
    if (this.websocket) {
      this.websocket.onopen = null;
      this.websocket.onmessage = null;
      this.websocket.onclose = null;
      this.websocket.onerror = null;
      this.websocket.close();
    }

    const username = this.resolvedUsername();
    this.webUsername = username;
    const generation = this.webGeneration = (this.webGeneration || 0) + 1;
    let ticket;
    try { ticket = await window.faySecurity.ticket(username, 'web'); }
    catch (error) { this.connectionError(error, 'web'); return; }
    if (this.destroyed || generation !== this.webGeneration) return;
    this.websocket = new WebSocket(`${this.baseWsUrl}/?ticket=${encodeURIComponent(ticket)}`);

    this.websocket.onopen = () => {
      console.log('WebSocket connection opened');
    };

    this.websocket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      this.handleIncomingMessage(data);
    };

    this.websocket.onclose = () => {
      console.log('WebSocket connection closed. Attempting to reconnect...');
      if (!this.destroyed) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => this.connectWebSocket(), 5000);
      }
    };

    this.websocket.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.reconnectTimer);
    clearTimeout(this.humanReconnectTimer);
    document.removeEventListener('pointerdown', this.unlockAudioHandler);

    [this.websocket, this.humanWebsocket].forEach((socket) => {
      if (!socket) {
        return;
      }
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      try {
        socket.close();
      } catch (error) {
        console.warn('Failed to close WebSocket cleanly:', error);
      }
    });

    this.audioQueue = [];
    this.currentAudioItem = null;
    this.audioPlayer.pause();
    this.audioPlayer.removeAttribute('src');
    this.audioPlayer.load();
    this.audioPlayer.onplaying = null;
    this.audioPlayer.onended = null;
    this.audioPlayer.onerror = null;
    if (this.audioContext && this.audioContext.close) {
      this.audioContext.close().catch(() => {});
    }
  }

  async fetchData(url, options = {}) {
    try {
      // Ensure headers are properly set for POST requests
      if (options.method === 'POST') {
        options.headers = {
          'Content-Type': 'application/json',
          ...options.headers
        };
      }
      
      const response = await fetch(url, options);
      if (!response.ok) {
        throw new Error(`HTTP error! Status: ${response.status}`);
      }
      const data = await response.json();
      return data;
    } catch (error) {
      console.error('Error fetching data:', error);
      throw error; // Rethrow to handle in the calling function
    }
  }

  getVoiceList() {
    return this.fetchData(`${this.baseApiUrl}/api/get-voice-list`);
  }

  getAudioDeviceList() {
    return this.fetchData(`${this.baseApiUrl}/api/get-audio-device-list`);
  }

  submitConfig(config) {
    return this.fetchData(`${this.baseApiUrl}/api/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config })
    });
  }

  controlEyes(state) {
    return this.fetchData(`${this.baseApiUrl}/api/control-eyes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state })
    });
  }

  startLive() {
    return this.fetchData(`${this.baseApiUrl}/api/start-live`, {
      method: 'POST'
    });
  }

  stopLive() {
    return this.fetchData(`${this.baseApiUrl}/api/stop-live`, {
      method: 'POST'
    });
  }

  getRunStatus() {
    return this.fetchData(`${this.baseApiUrl}/api/get-run-status`, {
      method: 'POST'
    });
  }

  getMessageHistory(username, limit = 30, offset = 0) {
    return new Promise((resolve, reject) => {
      const url = `${this.baseApiUrl}/api/get-msg`;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("Content-type", "application/x-www-form-urlencoded");
      const send_data = `data=${encodeURIComponent(JSON.stringify({ username, limit, offset }))}`;
      xhr.send(send_data);

      xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
          if (xhr.status === 200) {
            try {
              const data = JSON.parse(xhr.responseText);
              if (data && data.list) {
                const combinedList = data.list.flat();
                resolve({
                  list: combinedList,
                  total: data.total || 0,
                  hasMore: data.hasMore || false
                });
              } else {
                resolve({ list: [], total: 0, hasMore: false });
              }
            } catch (e) {
              console.error('Error parsing response:', e);
              reject(e);
            }
          } else {
            reject(new Error(`Request failed with status ${xhr.status}`));
          }
        }
      };
    });
  }

  getUserList() {
    return this.fetchData(`${this.baseApiUrl}/api/get-member-list`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
  }

  getData() {
    return this.fetchData(`${this.baseApiUrl}/api/get-data`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
}

  getTime(){
    const date = new Date();
    const year = date.getFullYear();
    const month = (date.getMonth() + 1).toString().padStart(2, '0'); // 月份从0开始，需要+1
    const day = date.getDate().toString().padStart(2, '0');
    const hours = date.getHours().toString().padStart(2, '0');
    const minutes = date.getMinutes().toString().padStart(2, '0');
    const seconds = date.getSeconds().toString().padStart(2, '0');
    const milliseconds = date.getMilliseconds().toString().padStart(3, '0');
    const currentDateTimeWithMs = `${year}-${month}-${day} ${hours}:${minutes}:${seconds}.${milliseconds}`;
    return currentDateTimeWithMs
  }

  handleIncomingMessage(data) {
    const vueInstance = this.vueInstance; 
  //   console.log('Incoming message:', data);
    if (data.liveState !== undefined) {
      vueInstance.liveState = data.liveState;
      if (data.liveState === 1) {
        vueInstance.configEditable = false;
      } else if (data.liveState === 0) {
        vueInstance.configEditable = true;
      }
    }

    if (data.voiceList !== undefined) {
      vueInstance.voiceList = data.voiceList.map(voice => ({
        value: voice.id,
        label: voice.name
      }));
    }

    if (data.deviceList !== undefined) {
      vueInstance.deviceList = data.deviceList.map(device => ({
        value: device,
        label: device
      }));
    }

    if (data.panelMsg !== undefined) {
      vueInstance.panelMsg = data.panelMsg; 
    
    }
    if (data.robot) {
      vueInstance.$set(vueInstance, 'robot', window.FAY_CUSTOMER_AVATAR_URL || '/static/images/xiaoru-avatar.svg');
      }
    if (data.agentPresentation && vueInstance.syncChatStylePresentation) {
      if (Array.isArray(vueInstance.selectedUser) && data.agentPresentation.username === vueInstance.selectedUser[1]) {
        vueInstance.syncChatStylePresentation(data.agentPresentation.revision);
      }
    }
    if (data.agentToolProgress && vueInstance.chatStylePending) vueInstance.chatStyleProgress = data.agentToolProgress.message;
    if (data.panelReply !== undefined) {
      if (data.panelReply.type === 'fay' && data.panelReply.is_end &&
          Array.isArray(vueInstance.selectedUser) && data.panelReply.username === vueInstance.selectedUser[1]) {
        vueInstance.chatStylePending = false;
        vueInstance.chatStyleProgress = '';
        clearTimeout(vueInstance.chatStyleTimer);
      }
      if (data.panelReply.type === 'fay' && vueInstance.scheduleStyleSync && vueInstance.styleInitialized) {
        vueInstance.scheduleStyleSync();
      }
      console.log('收到消息:', data.panelReply);
      vueInstance.panelReply = data.panelReply.content;
      
      // 更新用户列表
      const userExists = vueInstance.userList.some(user => user[1] === data.panelReply.username);
      if (!userExists) {
        vueInstance.userList.push([data.panelReply.uid, data.panelReply.username]);
      }

      if (vueInstance.selectedUser && data.panelReply.username === vueInstance.selectedUser[1]) {
        // 查找是否已存在相同content_id的消息
        const existingMessageIndex = vueInstance.messages.findIndex(
          msg => msg.id === data.panelReply.id && msg.type === data.panelReply.type
        );
        
        if (existingMessageIndex !== -1) {
          // 更新现有消息（拼接内容）
          const existingMessage = vueInstance.messages[existingMessageIndex];
          if (data.panelReply.conversation_id) existingMessage.conversation_id = data.panelReply.conversation_id;
          if (vueInstance.attachChatStylePresentation) vueInstance.attachChatStylePresentation(existingMessage);
          // 拼接新内容到现有内容
          existingMessage.content = existingMessage.content + data.panelReply.content;
          existingMessage.timetext = data.panelReply.timetext || this.getTime();

          // 检测 think 标签状态
          const hasThinkStart = existingMessage.content.includes('<think>');
          const hasThinkEnd = existingMessage.content.includes('</think>');
          if (hasThinkStart && !hasThinkEnd) {
            // think 正在接收中，展开并显示加载状态
            vueInstance.$set(existingMessage, 'thinkExpanded', true);
            vueInstance.$set(existingMessage, 'thinkLoading', true);
          } else if (hasThinkStart && hasThinkEnd) {
            // think 接收完成，关闭加载状态
            vueInstance.$set(existingMessage, 'thinkLoading', false);
          }

          // 防抖更新视图，减少渲染频率避免PyQt卡死
          if (!this._streamDebounceTimers) this._streamDebounceTimers = {};
          const timerKey = existingMessage.id + '_' + existingMessage.type;
          if (this._streamDebounceTimers[timerKey]) {
            clearTimeout(this._streamDebounceTimers[timerKey]);
          }
          this._streamDebounceTimers[timerKey] = setTimeout(() => {
            vueInstance.$forceUpdate();
            delete this._streamDebounceTimers[timerKey];
          }, 150);

          // 流式结束兜底：500ms无新chunk时从数据库拉取完整内容，确保markdown正确渲染
          if (!this._streamEndTimers) this._streamEndTimers = {};
          const endKey = existingMessage.id + '_end';
          if (this._streamEndTimers[endKey]) {
            clearTimeout(this._streamEndTimers[endKey]);
          }
          this._streamEndTimers[endKey] = setTimeout(() => {
            // 从后端拉取完整消息内容
            fetch(`${this.baseApiUrl}/api/get-msg-by-id`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: existingMessage.id })
            })
            .then(r => r.json())
            .then(res => {
              if (res.content) {
                existingMessage.content = res.content;
                vueInstance.$forceUpdate();
              }
            })
            .catch(() => {});
            delete this._streamEndTimers[endKey];
          }, 500);
        } else {
          // 添加新消息
          const newMessage = {
            id: data.panelReply.id,
            username: data.panelReply.username,
            content: data.panelReply.content,
            type: data.panelReply.type,
            timetext: data.panelReply.timetext || this.getTime(),
            is_adopted: data.panelReply.is_adopted ? 1 : 0,
            thinkExpanded: false,
            thinkLoading: false,
            conversation_id: data.panelReply.conversation_id, presentation: null
          };

          // 检测新消息是否包含 think 开始标签
          if (newMessage.content.includes('<think>') && !newMessage.content.includes('</think>')) {
            newMessage.thinkExpanded = true;
            newMessage.thinkLoading = true;
          }

          vueInstance.messages.push(newMessage);
          if (vueInstance.attachChatStylePresentation) vueInstance.attachChatStylePresentation(newMessage);
        }

        // 滚动到底部
        vueInstance.$nextTick(() => {
          const chatContainer = vueInstance.$el.querySelector('.chatmessage');
          if (chatContainer) {
            chatContainer.scrollTop = chatContainer.scrollHeight;
          }
        });
      }
    }

    if (data.is_connect !== undefined) {
      vueInstance.isConnected = data.is_connect;
    }

    if (data.remote_audio_connect !== undefined) {
      vueInstance.remoteAudioConnected = data.remote_audio_connect;
    }
  }
}

const STOREFRONT_FALLBACK = {
  store: {
    name: '小如的衣橱体验店',
    subtitle: '原创演示目录 · 本地交易沙盒',
    dataset_label: '虚构演示商品（不代表真实在售）',
    reference_url: ''
  },
  account: {
    wallet_type: 'sandbox',
    initial_balance: 500,
    balance: 500,
    currency: 'CNY',
    store_address: '浙江省杭州市',
    customer_address: '河南省郑州市'
  },
  products: [],
  orders: [],
  logistics: [],
  sources: [
    { title: '中华人民共和国消费者权益保护法', publisher: '国家市场监督管理总局', url: 'https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_e6dfa55ac9404b9cb3ff92e51f6563b4.html' },
    { title: '中华人民共和国消费者权益保护法实施条例', publisher: '中国政府网', url: 'https://www.gov.cn/zhengce/202406/content_6955346.htm' },
    { title: '网络购买商品七日无理由退货暂行办法（2020年修订）', publisher: '国家市场监督管理总局', url: 'https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_9d1398f5fb8d4be9a4af863b56e8ae5a.html' },
    { title: '中华人民共和国电子商务法', publisher: '全国人民代表大会', url: 'https://www.npc.gov.cn/npc/c2/c30834/201905/t20190521_278179.html' },
    { title: '中华人民共和国个人信息保护法', publisher: '全国人民代表大会', url: 'https://www.npc.gov.cn/npc/c2/c30834/202108/t20210820_313088.html' },
    { title: '快递暂行条例（2024年修订）', publisher: '中国政府网', url: 'https://www.gov.cn/zhengce/202403/content_6942294.htm' }
  ]
};

function cloneStorefrontFallback() {
  return JSON.parse(JSON.stringify(STOREFRONT_FALLBACK));
}

new Vue({
  mixins: window.FayStylingMixin ? [window.FayStylingMixin] : [],
  el: '#app',
  delimiters: ["[[", "]]"],
  data() {
    return {
      messages: [],
      newMessage: '',
      commerceProductContext: null,
      fayService: null,
      liveState: 0,
      isConnected: false,
      remoteAudioConnected: false,
      userList: [],
      selectedUser: null,
      loading: false,
      chatMessages: {},
      panelMsg: '',
      panelReply: '',
      robot: window.FAY_CUSTOMER_AVATAR_URL || '/static/images/xiaoru-avatar.svg',
      base_url: window.location.protocol + '//' + window.location.hostname + ':' + window.location.port,
      hostname: window.location.hostname,
      play_sound_enabled: false,
      source_record_enabled: false,
      userListTimer: null,
      thinkPanelExpanded: true,
      thinkContent: '',
      isThinkPanelMinimized: false,
      mcpOnlineStatus: false,
      mcpCheckTimer: null,
      systemStatus: {
        server: false,
        digital_human: false,
        remote_audio: false
      },
      systemStatusTimer: null,
      audioConfigTimer: null,
      addUserDialogVisible: false,
      newUsername: '',
      extraInfoDialogVisible: false,
      editingUserForExtraInfo: null,
      editingExtraInfo: '',
      editingUserPortrait: '',
      // 分页相关
      messageOffset: 0,
      messageLimit: 30,
      hasMoreMessages: false,
      loadingMoreMessages: false,
      // 分享图相关
      shareSelectMode: false,
      sharePreviewVisible: false,
      shareImageUrl: '',
      // 后台任务侧边栏
      execPanelOpen: false,
      execStatus: { status: 'idle', original_request: '', current_step: '', steps_done: 0, elapsed: 0, error: null, progress_messages: [] },
      execStatusUsername: null,
      execPollTimer: null,
      // 电商业务沙盒（与 Fay 的聊天、语音和数字人链路保持解耦）
      storeMeta: cloneStorefrontFallback().store,
      storeAccount: cloneStorefrontFallback().account,
      storefrontTabs: [
        { key: 'products', label: '商品', icon: '▦' },
        { key: 'styling', label: '穿搭', icon: '✦' },
        { key: 'orders', label: '订单', icon: '▧' },
        { key: 'logistics', label: '物流', icon: '◇' },
        { key: 'afterSales', label: '售后', icon: '↺' }
      ],
      storeActiveTab: 'products',
      mobileWorkspaceTab: 'store',
      storeLoading: true,
      storeProducts: [],
      storeOrders: [],
      storeLogistics: [],
      storeReturns: [],
      storeCategories: [],
      storefrontSources: cloneStorefrontFallback().sources,
      productKeyword: '',
      productCategory: '全部',
      productPage: 1,
      productPageSize: 12,
      productTotal: 0,
      productPages: 0,
      productSearchTimer: null,
      catalogServerBacked: false,
      focusedOrderId: '',
      selectedProduct: null,
      selectedReturnOrder: null,
      sizeDialogVisible: false,
      orderDialogVisible: false,
      returnDialogVisible: false,
      tryOnDialogVisible: false,
      sourcesDialogVisible: false,
      demoSessionCsrf: '',
      sizeRecommendationLoading: false,
      sizeRecommendationResult: null,
      orderSubmitting: false,
      orderSimulationId: '',
      orderPreview: null,
      returnSubmitting: false,
      returnSimulationId: '',
      returnPreview: null,
      tryOnStatus: { configured: false, development_placeholder: false, max_upload_mb: 64, result_ttl_minutes: 30, privacy_notice: '' },
      tryOnLoading: false,
      tryOnDownloading: false,
      tryOnPhotoDragging: false,
      tryOnPhotoDragDepth: 0,
      tryOnReference: null,
      tryOnReferenceLoading: false,
      tryOnPhoto: null,
      tryOnPhotoPreview: '',
      tryOnResult: null,
      tryOnForm: { beautify: 'natural', consent: false },
      sizeForm: { height: 170, weight: 60, chest: null, shoulder: null, fit: '合身' },
      orderForm: { sku_id: '', quantity: 1 },
      returnForm: { reason: '不喜欢或不合适' },
    };
  },
  computed: {
    tryOnDisplayReference() {
      return (this.tryOnResult && this.tryOnResult.reference) || this.tryOnReference;
    },
    tryOnReferenceLabel() {
      if (this.tryOnReferenceLoading) return '正在核对本次试穿的衣服图…';
      const reference = this.tryOnDisplayReference;
      if (!reference) return '请等待试衣参考图加载';
      return reference.color_label
        ? `本次试穿：${reference.color_label}（参考图颜色）`
        : '本次试穿颜色以参考图为准';
    },
    shareSelectedCount() {
      return this.messages.filter(m => m.shareSelected).length;
    },
    shareSelectedMessages() {
      return this.messages.filter(m => m.shareSelected);
    },
    execStatusLabel() {
      var map = { idle: '空闲', running: '执行中', done: '已完成', failed: '失败', cancelled: '已取消' };
      return map[this.execStatus.status] || this.execStatus.status;
    },
    productCategories() {
      if (this.storeCategories.length) {
        return this.storeCategories.map(category => typeof category === 'string' ? category : category.name).filter(Boolean);
      }
      return Array.from(new Set(this.storeProducts.map(product => product.category).filter(Boolean)));
    },
    filteredProducts() {
      if (this.catalogServerBacked) {
        return this.storeProducts;
      }
      const keyword = String(this.productKeyword || '').trim().toLowerCase();
      return this.storeProducts.filter((product) => {
        if (this.productCategory !== '全部' && product.category !== this.productCategory) {
          return false;
        }
        if (!keyword) {
          return true;
        }
        const searchable = [
          product.product_id,
          product.name,
          product.category,
          ...(product.aliases || []),
          ...(product.highlights || [])
        ].join(' ').toLowerCase();
        return searchable.indexOf(keyword) !== -1;
      });
    },
    visibleOrders() {
      const username = this.selectedUser && this.selectedUser.length > 1 ? this.selectedUser[1] : 'User';
      return this.storeOrders.filter(order => !order.user_id || order.demo_only || order.user_id === 'demo-session' || order.user_id === username);
    },
    visibleLogistics() {
      const allowedOrderIds = new Set(this.visibleOrders.map(order => String(order.order_id || '').toUpperCase()));
      const records = this.storeLogistics.filter(record => allowedOrderIds.has(String(record.order_id || '').toUpperCase()));
      if (!this.focusedOrderId) {
        return records;
      }
      return records.slice().sort((left, right) => {
        if (left.order_id === this.focusedOrderId) return -1;
        if (right.order_id === this.focusedOrderId) return 1;
        return 0;
      });
    },
    selectedSku() {
      if (!this.selectedProduct || !Array.isArray(this.selectedProduct.skus)) {
        return null;
      }
      return this.selectedProduct.skus.find(sku => sku.sku_id === this.orderForm.sku_id) || null;
    },
    selectedSkuStock() {
      if (this.selectedSku) {
        return Number(this.selectedSku.stock || 0);
      }
      return this.selectedProduct ? this.productTotalStock(this.selectedProduct) : 0;
    },
    orderPreviewTotal() {
      if (!this.selectedProduct) {
        return 0;
      }
      const unitPrice = this.selectedSku && this.selectedSku.price != null
        ? this.selectedSku.price
        : this.selectedProduct.price;
      return Number(unitPrice || 0) * Number(this.orderForm.quantity || 1);
    },
    canAffordOrderPreview() {
      return !this.orderPreview || this.orderPreview.can_afford !== false;
    }
  },
  watch: {
    newMessage(value) {
      if (this.commerceProductContext && String(value || '').trim() !== this.commerceProductContext.prompt) {
        this.commerceProductContext = null;
      }
    },
    productKeyword() {
      if (this.productSearchTimer) {
        clearTimeout(this.productSearchTimer);
      }
      this.productSearchTimer = setTimeout(() => this.loadProducts(1, true), 320);
    },
    productCategory() {
      this.loadProducts(1, true);
    }
  },
  created() {
    this.initFayService();
    this.getData();
    this.startUserListTimer();
    this.checkMcpStatus();
    this.startMcpStatusTimer();
    this.startSystemStatusTimer();
    this.startAudioConfigSyncTimer();
    this.startExecPollTimer();
    this.loadStorefront();
    this.loadTryOnStatus();
  },
  mounted() {
    // Vue 接管 #app 后再创建 Pixi canvas，避免后续虚拟 DOM 首次渲染
    // 覆盖 Live2D 运行时插入的 canvas 和状态属性。
    if (window.initializeFayLive2D) {
      window.initializeFayLive2D();
    }
  },
  beforeDestroy() {
    [
      'userListTimer',
      'mcpCheckTimer',
      'systemStatusTimer',
      'audioConfigTimer',
      'execPollTimer',
      'productSearchTimer'
    ].forEach((timerName) => {
      if (this[timerName]) {
        clearInterval(this[timerName]);
        this[timerName] = null;
      }
    });
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.fayService && this.fayService.destroy) {
      this.fayService.destroy();
    }
    if (window.fayAvatar && window.fayAvatar.destroy) {
      window.fayAvatar.destroy();
    }
    this.releaseTryOnPhotoPreview();
  },
  methods: {
    normalizeStoreProduct(product) {
      const source = product || {};
      const variants = Array.isArray(source.skus) ? source.skus : (Array.isArray(source.variants) ? source.variants : []);
      return Object.assign({}, source, {
        product_id: source.product_id || source.id || '',
        name: source.name || '未命名商品',
        category: source.category || '其他',
        price: source.price == null ? 0 : source.price,
        highlights: Array.isArray(source.highlights) && source.highlights.length
          ? source.highlights
          : [source.selling_point || source.data_notice || '公开商品页快照；详情请以来源页面为准'].filter(Boolean),
        image: source.image || source.image_url || '',
        skus: variants.map((variant) => ({
          sku_id: variant.sku_id || variant.id || '',
          attributes: variant.attributes || {},
          stock: variant.stock == null ? variant.stock_public : variant.stock,
          price: variant.price,
          disabled: variant.disabled || variant.disabled_public || false
        }))
      });
    },

    applyStorefrontPayload(payload) {
      const fallback = cloneStorefrontFallback();
      const data = payload && payload.data && typeof payload.data === 'object' ? payload.data : (payload || {});
      this.storeMeta = Object.assign({}, fallback.store, data.store || {});
      this.storeAccount = Object.assign({}, fallback.account, data.account || {});
      const products = Array.isArray(data.products) && data.products.length ? data.products : fallback.products;
      this.storeProducts = products.map(product => this.normalizeStoreProduct(product));
      this.storeOrders = Array.isArray(data.orders) ? data.orders : fallback.orders;
      this.storeLogistics = Array.isArray(data.logistics) ? data.logistics : fallback.logistics;
      const sources = [...(Array.isArray(data.sources) ? data.sources : []), ...fallback.sources];
      const sourceUrls = new Set();
      this.storefrontSources = sources.filter((source) => {
        if (!source || !source.url || sourceUrls.has(source.url)) return false;
        sourceUrls.add(source.url);
        return true;
      });
    },

    loadStorefront(silent) {
      const username = this.selectedUser && this.selectedUser.length > 1 ? this.selectedUser[1] : 'User';
      if (!silent) {
        this.storeLoading = true;
      }
      return fetch(`${this.base_url}/api/ecommerce/storefront?username=${encodeURIComponent(username)}`, {
        headers: { 'Accept': 'application/json' }
      })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`storefront api ${response.status}`);
          }
          return response.json();
        })
        .then((payload) => {
          this.applyStorefrontPayload(payload);
          this.loadStoreCategories();
          this.loadProducts(1, true);
          this.loadDemoReturns();
          return payload;
        })
        .catch((error) => {
          // 后端尚未启用接口时，仍展示与 agent/data 一致的只读目录。
          this.applyStorefrontPayload(cloneStorefrontFallback());
          console.warn('商城接口暂不可用，已切换到前端目录数据:', error.message);
          return null;
        })
        .then((payload) => {
          this.storeLoading = false;
          return payload;
        });
    },

    loadStoreCategories() {
      return fetch(`${this.base_url}/api/store/categories`, { headers: { 'Accept': 'application/json' } })
        .then((response) => {
          if (!response.ok) throw new Error(`categories api ${response.status}`);
          return response.json();
        })
        .then((payload) => {
          const categories = payload && payload.data;
          if (Array.isArray(categories)) {
            this.storeCategories = categories;
          }
        })
        .catch(() => {});
    },

    loadDemoReturns() {
      return fetch(`${this.base_url}/api/store/demo-returns`, {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
      })
        .then((response) => {
          if (!response.ok) throw new Error(`returns api ${response.status}`);
          return response.json();
        })
        .then((payload) => {
          this.storeReturns = payload && Array.isArray(payload.data) ? payload.data : [];
        })
        .catch(() => {
          this.storeReturns = [];
        });
    },

    loadProducts(page, silent) {
      const targetPage = Math.max(Number(page || 1), 1);
      const params = new URLSearchParams({
        page: String(targetPage),
        page_size: String(this.productPageSize),
        q: String(this.productKeyword || '').trim()
      });
      if (this.productCategory && this.productCategory !== '全部') {
        params.set('category', this.productCategory);
      }
      if (!silent) {
        this.storeLoading = true;
      }
      return fetch(`${this.base_url}/api/store/products?${params.toString()}`, { headers: { 'Accept': 'application/json' } })
        .then((response) => {
          if (!response.ok) throw new Error(`products api ${response.status}`);
          return response.json();
        })
        .then((payload) => {
          const catalog = payload && payload.data ? payload.data : payload;
          if (!catalog || !Array.isArray(catalog.items)) {
            throw new Error('products api payload invalid');
          }
          this.storeProducts = catalog.items.map(product => this.normalizeStoreProduct(product));
          this.productPage = Number(catalog.page || targetPage);
          this.productTotal = Number(catalog.total || this.storeProducts.length);
          this.productPages = Number(catalog.pages || Math.ceil(this.productTotal / this.productPageSize));
          this.catalogServerBacked = true;
        })
        .catch((error) => {
          this.catalogServerBacked = false;
          console.warn('商品分页接口暂不可用，保留当前演示目录:', error.message);
        })
        .then(() => {
          this.storeLoading = false;
        });
    },

    async ensureDemoStoreSession(forceRefresh) {
      if (this.demoSessionCsrf && !forceRefresh) {
        return this.demoSessionCsrf;
      }
      const response = await fetch(`${this.base_url}/api/store/session`, {
        method: 'GET',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok || !payload.data || !payload.data.csrf_token) {
        throw new Error(payload.message || '无法初始化购物会话');
      }
      this.demoSessionCsrf = payload.data.csrf_token;
      if (payload.data.account) {
        this.storeAccount = Object.assign({}, this.storeAccount, payload.data.account);
      }
      return this.demoSessionCsrf;
    },

    async storeWrite(path, body) {
      const csrfToken = await this.ensureDemoStoreSession(false);
      const response = await fetch(`${this.base_url}${path}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          'X-Fay-Demo-CSRF': csrfToken
        },
        body: JSON.stringify(body || {})
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.message || `购物服务请求失败（${response.status}）`);
        error.code = payload.error || '';
        throw error;
      }
      return payload.data;
    },

    async loadTryOnStatus(productId) {
      const isCurrentProduct = () => !productId || (
        this.tryOnDialogVisible && this.selectedProduct &&
        this.selectedProduct.product_id === productId
      );
      try {
        const query = productId ? `?product_id=${encodeURIComponent(productId)}` : '';
        const response = await fetch(`${this.base_url}/api/tryon/status${query}`, {
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' }
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) throw new Error(payload.message || '无法读取试衣服务状态');
        if (!isCurrentProduct()) return;
        this.tryOnStatus = Object.assign({}, this.tryOnStatus, payload.data || {});
        if (productId) this.tryOnReference = payload.data.reference || null;
      } catch (error) {
        if (!isCurrentProduct()) return;
        this.tryOnStatus = Object.assign({}, this.tryOnStatus, { configured: false });
        if (productId) this.$message.error(error.message || '无法核对试衣参考图');
      } finally {
        if (productId && isCurrentProduct()) this.tryOnReferenceLoading = false;
      }
    },

    async prepareSingleProductCart(product, quantity) {
      await this.ensureDemoStoreSession(false);
      const cartResponse = await fetch(`${this.base_url}/api/store/cart`, {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
      });
      const cartPayload = await cartResponse.json().catch(() => ({}));
      const currentItems = cartPayload && cartPayload.data && Array.isArray(cartPayload.data.items)
        ? cartPayload.data.items
        : [];
      for (const item of currentItems) {
        await this.storeWrite('/api/store/cart/items', {
          product_id: item.product_id,
          sku_id: item.sku_id || null,
          quantity: 0
        });
      }
      return this.storeWrite('/api/store/cart/items', {
        product_id: product.product_id,
        sku_id: this.orderForm.sku_id || null,
        quantity: Number(quantity || 1)
      });
    },

    createIdempotencyKey(prefix) {
      const randomPart = Math.random().toString(36).slice(2, 10);
      return `${prefix}-${Date.now()}-${randomPart}`;
    },

    switchStoreTab(tabKey) {
      this.storeActiveTab = tabKey;
      this.mobileWorkspaceTab = 'store';
      if (tabKey === 'styling' && this.loadStyleAdvisor) this.loadStyleAdvisor();
    },

    sendCommercePrompt(text, commerceProductId = null) {
      const prompt = String(text || '').trim();
      if (!prompt) {
        return;
      }
      this.mobileWorkspaceTab = 'service';
      this.newMessage = prompt;
      this.commerceProductContext = commerceProductId
        ? { prompt, productId: String(commerceProductId) }
        : null;
      if (this.liveState !== 1) {
        this.$message.warning('已把问题放入输入框，请先开启服务后发送');
        return;
      }
      this.$nextTick(() => {
        if (this.newMessage === prompt) this.sendMessage();
      });
    },

    askProduct(product) {
      this.sendCommercePrompt(`这款商品「${product.name}」怎么样？`, product.product_id);
    },

    askOrder(order) {
      this.sendCommercePrompt(`我的订单 ${order.order_id} 现在是什么状态？`);
    },

    askLogistics(record) {
      this.sendCommercePrompt(`订单 ${record.order_id} 的物流到哪里了？`);
    },

    openLogistics(order) {
      this.focusedOrderId = order.order_id;
      this.switchStoreTab('logistics');
      if (!this.storeLogistics.some(record => record.order_id === order.order_id)) {
        this.$message.info('该订单暂无物流轨迹，可以向数字客服继续咨询');
      }
    },

    orderFor(orderId) {
      return this.visibleOrders.find(order => order.order_id === orderId) || null;
    },

    canAdvanceDemoOrder(order) {
      return Boolean(order && order.demo_only && !['delivered', 'closed', 'cancelled'].includes(order.status));
    },

    canOpenReturn(order) {
      return Boolean(
        order
        && order.demo_only
        && ['delivered', 'closed'].includes(order.status)
        && !this.hasReturnForOrder(order.order_id)
      );
    },

    hasReturnForOrder(orderId) {
      const normalizedOrderId = String(orderId || '').toUpperCase();
      return Boolean(normalizedOrderId && this.storeReturns.some((record) => (
        String(record.order_id || '').toUpperCase() === normalizedOrderId
      )));
    },

    returnActionText(order, withArrow) {
      if (this.hasReturnForOrder(order && order.order_id)) {
        return '已有售后记录';
      }
      if (this.canOpenReturn(order)) {
        return withArrow ? '申请售后 →' : '申请售后';
      }
      return '签收后可售后';
    },

    async advanceDemoOrder(orderId) {
      const order = this.orderFor(orderId);
      if (!order || !this.canAdvanceDemoOrder(order)) {
        this.$message.info('该订单已到最终节点，不能继续推进');
        return;
      }
      try {
        const updated = await this.storeWrite(`/api/store/demo-orders/${encodeURIComponent(orderId)}/advance`, {});
        await this.loadStorefront(true);
        this.focusedOrderId = orderId;
        this.switchStoreTab('logistics');
        this.$message.success(`订单进度已更新至：${updated.status_text || this.formatOrderStatus(updated.status)}`);
      } catch (error) {
        this.$message.error(error.message || '无法推进订单物流');
      }
    },

    canCancelOrder(order) {
      return Boolean(order && ['demo_created', 'awaiting_demo_fulfillment'].includes(order.status));
    },

    async cancelSandboxOrder(order) {
      if (!this.canCancelOrder(order)) return;
      try {
        await this.$confirm(
          `确认取消订单 ${this.displayOrderNo(order.order_id)}？已支付金额将退回体验余额。`,
          '取消订单',
          { confirmButtonText: '确认取消并退款', cancelButtonText: '暂不取消', type: 'warning' }
        );
      } catch (error) {
        return;
      }
      try {
        const updated = await this.storeWrite(`/api/store/demo-orders/${encodeURIComponent(order.order_id)}/cancel`, {});
        await this.loadStorefront(true);
        this.$message.success(`订单已取消，¥${this.formatMoney(updated.refunded_amount)} 已退回体验余额`);
      } catch (error) {
        this.$message.error(error.message || '取消订单失败');
      }
    },

    async simulateOrderJourney(orderId) {
      if (this.orderSimulationId) return;
      this.orderSimulationId = orderId;
      try {
        let status = (this.orderFor(orderId) || {}).status;
        const advanceable = new Set(['demo_created', 'awaiting_demo_fulfillment', 'shipped', 'in_transit']);
        let steps = 0;
        while (advanceable.has(status) && steps < 5) {
          const updated = await this.storeWrite(`/api/store/demo-orders/${encodeURIComponent(orderId)}/advance`, {});
          status = updated.status;
          steps += 1;
          if (status === 'delivered') break;
          await new Promise(resolve => setTimeout(resolve, 360));
        }
        await this.loadStorefront(true);
        this.focusedOrderId = orderId;
        this.switchStoreTab('logistics');
        this.$message.success('杭州发货至河南签收的配送流程已模拟完成');
      } catch (error) {
        this.$message.error(error.message || '完整配送模拟失败');
      } finally {
        this.orderSimulationId = '';
      }
    },

    canAdvanceReturn(record) {
      return Boolean(
        record
        && !record.manual_review_required
        && !['rejected', 'demo_refund_completed'].includes(record.status)
      );
    },

    async simulateReturnJourney(record) {
      if (!this.canAdvanceReturn(record) || this.returnSimulationId) return;
      this.returnSimulationId = record.return_id;
      try {
        let status = record.status;
        let updated = record;
        const terminal = new Set(['rejected', 'demo_refund_completed']);
        let steps = 0;
        while (!terminal.has(status) && steps < 7) {
          updated = await this.storeWrite(`/api/store/demo-returns/${encodeURIComponent(record.return_id)}/advance`, {});
          status = updated.status;
          steps += 1;
          if (terminal.has(status)) break;
          await new Promise(resolve => setTimeout(resolve, 320));
        }
        await Promise.all([this.loadStorefront(true), this.loadDemoReturns()]);
        this.switchStoreTab('afterSales');
        this.$message.success(`售后流程已完成，¥${this.formatMoney(updated.refund_amount)} 已退回体验余额`);
      } catch (error) {
        this.$message.error(error.message || '售后流程模拟失败');
      } finally {
        this.returnSimulationId = '';
      }
    },

    openSizeAdvisor(product) {
      this.selectedProduct = product;
      this.sizeForm = { height: 170, weight: 60, chest: null, shoulder: null, fit: '合身' };
      this.sizeRecommendationResult = null;
      this.sizeDialogVisible = true;
    },

    openTryOn(product) {
      this.selectedProduct = product;
      this.tryOnForm = { beautify: 'natural', consent: false };
      this.tryOnResult = null;
      this.tryOnReference = null;
      this.tryOnReferenceLoading = true;
      this.clearTryOnPhoto();
      this.tryOnDialogVisible = true;
      this.loadTryOnStatus(product.product_id);
    },

    async closeTryOnDialog(done) {
      if (this.tryOnDownloading) {
        this.$message.info('正在准备下载，请稍后关闭');
        return;
      }
      if (this.tryOnLoading) {
        this.$message.info('试穿效果正在生成，请完成后再关闭');
        return;
      }
      try {
        if (this.tryOnResult && this.tryOnResult.job_id) {
          await this.deleteTryOnResult(true);
        }
      } catch (error) {
        console.warn('关闭试衣弹窗时清理临时结果失败:', error.message);
      } finally {
        this.clearTryOnPhoto();
        if (typeof done === 'function') done();
        else this.tryOnDialogVisible = false;
      }
    },

    releaseTryOnPhotoPreview() {
      if (this.tryOnPhotoPreview && this.tryOnPhotoPreview.startsWith('blob:')) {
        URL.revokeObjectURL(this.tryOnPhotoPreview);
      }
      this.tryOnPhotoPreview = '';
    },

    clearTryOnPhoto() {
      this.releaseTryOnPhotoPreview();
      this.tryOnPhoto = null;
      this.resetTryOnPhotoDrag();
      if (this.$refs.tryOnPhotoInput) this.$refs.tryOnPhotoInput.value = '';
    },

    chooseTryOnPhoto() {
      if (!this.tryOnLoading && this.$refs.tryOnPhotoInput) this.$refs.tryOnPhotoInput.click();
    },

    resetTryOnPhotoDrag() {
      this.tryOnPhotoDragging = false;
      this.tryOnPhotoDragDepth = 0;
    },

    handleTryOnPhotoDragEnter(event) {
      if (this.tryOnLoading || !event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes('Files')) return;
      this.tryOnPhotoDragDepth += 1;
      this.tryOnPhotoDragging = true;
    },

    handleTryOnPhotoDragOver(event) {
      if (!event.dataTransfer) return;
      const acceptsFiles = Array.from(event.dataTransfer.types || []).includes('Files');
      event.dataTransfer.dropEffect = !this.tryOnLoading && acceptsFiles ? 'copy' : 'none';
      if (!this.tryOnLoading && acceptsFiles) this.tryOnPhotoDragging = true;
    },

    handleTryOnPhotoDragLeave() {
      this.tryOnPhotoDragDepth = Math.max(0, this.tryOnPhotoDragDepth - 1);
      if (!this.tryOnPhotoDragDepth) this.tryOnPhotoDragging = false;
    },

    handleTryOnPhotoDrop(event) {
      this.resetTryOnPhotoDrag();
      if (this.tryOnLoading) return;
      const files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      if (files.length !== 1) {
        this.$message.warning('每次请选择一张全身照片');
        return;
      }
      this.setTryOnPhoto(files[0]);
    },

    handleTryOnPhoto(event) {
      const file = event && event.target && event.target.files ? event.target.files[0] : null;
      this.setTryOnPhoto(file);
    },

    setTryOnPhoto(file) {
      if (this.tryOnLoading || !file) return;
      const allowed = ['image/jpeg', 'image/png', 'image/webp'];
      if (!allowed.includes(file.type)) {
        this.$message.error('仅支持 JPEG、PNG 或 WebP 图片');
        return;
      }
      const maxBytes = Number(this.tryOnStatus.max_upload_mb || 64) * 1024 * 1024;
      if (file.size > maxBytes) {
        this.$message.error(`照片不能超过 ${this.tryOnStatus.max_upload_mb || 64}MB`);
        return;
      }
      if (!file.size) {
        this.$message.error('照片文件为空，请重新选择');
        return;
      }
      if (this.tryOnResult && this.tryOnResult.job_id) {
        this.deleteTryOnResult(true).catch(error => {
          console.warn('更换照片时清理上一次试衣结果失败:', error.message);
        });
        this.tryOnResult = null;
      }
      this.clearTryOnPhoto();
      this.tryOnForm.consent = false;
      this.tryOnPhoto = file;
      this.tryOnPhotoPreview = URL.createObjectURL(file);
    },

    async submitTryOn() {
      if (this.tryOnLoading || this.tryOnDownloading) return;
      if (this.tryOnReferenceLoading || !this.tryOnReference) {
        this.$message.warning('请先等待试衣参考图核对完成');
        return;
      }
      if (!this.tryOnStatus.configured) {
        this.$message.warning('AI 虚拟试衣服务暂时不可用');
        return;
      }
      if (!this.selectedProduct || !this.tryOnPhoto) {
        this.$message.warning('请先上传一张清晰全身照片');
        return;
      }
      if (!this.tryOnForm.consent) {
        this.$message.warning('请先确认照片处理与临时存储说明');
        return;
      }
      this.resetTryOnPhotoDrag();
      this.tryOnLoading = true;
      try {
        if (this.tryOnResult && this.tryOnResult.job_id) await this.deleteTryOnResult(true);
        const csrfToken = await this.ensureDemoStoreSession(false);
        const form = new FormData();
        form.append('photo', this.tryOnPhoto, this.tryOnPhoto.name || 'photo.jpg');
        form.append('product_id', this.selectedProduct.product_id);
        form.append('beautify', this.tryOnForm.beautify);
        form.append('consent', 'true');
        const response = await fetch(`${this.base_url}/api/tryon/jobs`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json', 'X-Fay-Demo-CSRF': csrfToken },
          body: form
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) throw new Error(payload.message || `虚拟试衣请求失败（${response.status}）`);
        this.tryOnResult = payload.data;
        this.$message.success(payload.data.development_placeholder ? '试衣联调预览已生成' : 'AI 试衣效果已生成');
      } catch (error) {
        this.$message.error(error.message || '虚拟试衣暂不可用');
      } finally {
        this.tryOnLoading = false;
      }
    },

    tryOnResultUrl(download) {
      if (!this.tryOnResult || !this.tryOnResult.result_url) return '';
      return `${this.base_url}${this.tryOnResult.result_url}${download ? '?download=1' : ''}`;
    },

    async downloadTryOnResult() {
      if (this.tryOnDownloading) return;
      const result = this.tryOnResult;
      const url = this.tryOnResultUrl(true);
      if (!url) return;
      this.tryOnDownloading = true;
      let objectUrl = '';
      try {
        const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          if (response.status === 404) {
            if (this.tryOnResult === result) this.tryOnResult = null;
            throw new Error('试衣结果已过期、已删除或服务已重启，请重新生成后下载');
          }
          throw new Error(payload.message || `下载失败（${response.status}）`);
        }
        if (!(response.headers.get('Content-Type') || '').toLowerCase().startsWith('image/jpeg')) {
          throw new Error('下载内容不是有效的 JPG 图片，请重新生成后再试');
        }
        const blob = await response.blob();
        if (!blob.size) throw new Error('下载图片为空，请重新生成后再试');
        objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = `fay-tryon-${result.product_id || 'result'}.jpg`;
        document.body.appendChild(link);
        try {
          link.click();
        } finally {
          link.remove();
        }
        this.$message.info('已发起下载，请选择保存位置或查看浏览器下载列表');
      } catch (error) {
        this.$message.error(error.message || '下载失败，请检查连接后重试');
      } finally {
        // Qt 的保存窗口可能尚未关闭，延后释放地址以便浏览器接管下载。
        if (objectUrl) setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
        this.tryOnDownloading = false;
      }
    },

    async deleteTryOnResult(silent) {
      if (!this.tryOnResult || !this.tryOnResult.job_id) return;
      const jobId = this.tryOnResult.job_id;
      try {
        const csrfToken = await this.ensureDemoStoreSession(false);
        const response = await fetch(`${this.base_url}/api/tryon/jobs/${encodeURIComponent(jobId)}`, {
          method: 'DELETE',
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json', 'X-Fay-Demo-CSRF': csrfToken }
        });
        const payload = await response.json().catch(() => ({}));
        const alreadyGone = response.status === 404 && payload.error === 'tryon_result_not_found';
        if ((!response.ok || !payload.ok) && !alreadyGone) throw new Error(payload.message || '删除试衣结果失败');
        if (this.tryOnResult && this.tryOnResult.job_id === jobId) this.tryOnResult = null;
        if (!silent) this.$message.success('试衣照片和结果已删除');
      } catch (error) {
        if (!silent) this.$message.error(error.message || '删除试衣结果失败');
        else throw error;
      }
    },

    async submitSizeRecommendation() {
      if (!this.selectedProduct || !this.sizeForm.height || !this.sizeForm.weight) {
        this.$message.warning('请填写身高和体重');
        return;
      }
      this.sizeRecommendationLoading = true;
      this.sizeRecommendationResult = null;
      try {
        const data = await this.storeWrite('/api/ecommerce/size-recommendation', {
          product_id: this.selectedProduct.product_id,
          height_cm: this.sizeForm.height,
          weight_kg: this.sizeForm.weight,
          chest_cm: this.sizeForm.chest,
          shoulder_cm: this.sizeForm.shoulder,
          fit_preference: this.sizeForm.fit
        });
        this.sizeRecommendationResult = data;
      } catch (error) {
        this.sizeRecommendationResult = {
          error: true,
          message: error.message || '尺码工具暂不可用，请改由人工客服核对商品专属尺码表。',
          recommendations: [],
          privacy_notice: '本次身体参数未写入前端长期存储。'
        };
      } finally {
        this.sizeRecommendationLoading = false;
      }
    },

    openOrderPreview(product) {
      this.selectedProduct = product;
      const firstAvailableSku = (product.skus || []).find(sku => !sku.disabled && Number(sku.stock || 0) > 0);
      this.orderForm = { sku_id: firstAvailableSku ? firstAvailableSku.sku_id : '', quantity: 1 };
      this.orderPreview = null;
      this.orderDialogVisible = true;
    },

    async submitDemoOrder() {
      if (!this.selectedProduct) {
        return;
      }
      const productHasVariants = Array.isArray(this.selectedProduct.skus) && this.selectedProduct.skus.length > 0;
      if (productHasVariants && !this.selectedSku) {
        this.$message.warning('请选择有库存的规格');
        return;
      }
      if (Number(this.orderForm.quantity || 0) > this.selectedSkuStock) {
        this.$message.warning('购买数量不能超过当前库存');
        return;
      }
      this.orderSubmitting = true;
      try {
        if (!this.orderPreview) {
          await this.prepareSingleProductCart(this.selectedProduct, this.orderForm.quantity);
          this.orderPreview = await this.storeWrite('/api/store/demo-orders/preview', {});
          if (this.orderPreview.account) {
            this.storeAccount = Object.assign({}, this.storeAccount, this.orderPreview.account);
          }
          this.$message.success(this.orderPreview.can_afford === false ? '体验余额不足，请调整商品或数量' : '订单信息已核对，请确认支付');
          return;
        }
        if (this.orderPreview.can_afford === false) {
          this.$message.warning('体验余额不足，无法支付本单');
          return;
        }
        const createdOrder = await this.storeWrite('/api/store/demo-orders', {
          confirmation_token: this.orderPreview.confirmation_token,
          idempotency_key: this.createIdempotencyKey('sandbox-order')
        });
        this.orderDialogVisible = false;
        this.orderPreview = null;
        await this.loadStorefront(true);
        this.switchStoreTab('orders');
        const route = createdOrder.shipping || this.storeAccount;
        this.$alert(`订单 ${this.displayOrderNo(createdOrder.order_id)} 已使用体验余额支付，将从${route.from || route.store_address || '杭州'}发往${route.to || route.customer_address || '河南'}。`, '下单成功', {
          confirmButtonText: '查看订单',
          type: 'success'
        });
      } catch (error) {
        this.$message.error(error.message || '下单失败');
      } finally {
        this.orderSubmitting = false;
      }
    },

    openReturnPreview(order) {
      this.selectedReturnOrder = order;
      this.returnForm = { reason: '不喜欢或不合适' };
      this.returnPreview = null;
      this.returnDialogVisible = true;
    },

    async submitReturnPreview() {
      if (!this.selectedReturnOrder) {
        return;
      }
      const firstItem = this.selectedReturnOrder.items && this.selectedReturnOrder.items[0];
      if (!firstItem || !firstItem.order_item_id) {
        this.$message.warning('该订单不属于当前购物账户，不能提交售后');
        return;
      }
      this.returnSubmitting = true;
      try {
        if (!this.returnPreview) {
          this.returnPreview = await this.storeWrite('/api/store/demo-returns/preview', {
            order_id: this.selectedReturnOrder.order_id,
            order_item_id: firstItem.order_item_id,
            quantity: 1,
            reason_code: this.returnReasonCode(this.returnForm.reason)
          });
          this.$message.success('售后资格已核验，请再次确认');
          return;
        }
        const createdReturn = await this.storeWrite('/api/store/demo-returns', {
          confirmation_token: this.returnPreview.confirmation_token,
          idempotency_key: this.createIdempotencyKey('sandbox-return')
        });
        this.returnDialogVisible = false;
        this.returnPreview = null;
        await this.loadStorefront(true);
        this.switchStoreTab('afterSales');
        const reviewNotice = createdReturn.manual_review_required
          ? '本单达到大额订单复核条件，需要转人工确认。'
          : '';
        this.$alert(`售后申请 ${this.displayReturnNo(createdReturn.return_id)} 已提交。${reviewNotice}`, '售后申请已提交', {
          confirmButtonText: '知道了',
          type: 'success'
        });
      } catch (error) {
        this.$message.error(error.message || '售后申请失败');
      } finally {
        this.returnSubmitting = false;
      }
    },

    returnReasonCode(reason) {
      const map = {
        '不喜欢或不合适': 'size_not_suitable',
        '商品质量问题': 'quality_issue',
        '错发或漏发': 'wrong_item',
        '物流异常': 'damaged',
        '其他原因': 'other'
      };
      return map[reason] || 'other';
    },

    productTotalStock(product) {
      if (!product) return null;
      if (product.stock_total_public != null) {
        return Number(product.stock_total_public || 0);
      }
      const knownStocks = (Array.isArray(product.skus) ? product.skus : [])
        .map(sku => sku.stock)
        .filter(stock => stock !== null && stock !== undefined && stock !== '');
      if (!knownStocks.length) {
        return null;
      }
      return knownStocks.reduce((total, stock) => total + Number(stock || 0), 0);
    },

    formatProductStock(product) {
      const stock = this.productTotalStock(product);
      if (stock === null) return '库存待核验';
      return `库存 ${stock}`;
    },

    canDemoOrderProduct(product) {
      const stock = this.productTotalStock(product);
      return stock !== null && stock > 0;
    },

    isApparelProduct(product) {
      const text = `${product && product.category || ''} ${product && product.name || ''}`;
      return /(服饰|服装|女装|男装|童装|上衣|外套|衬衫|裙|裤|鞋|卫衣|毛衣|针织|尺码)/.test(text);
    },

    productCategoryFor(productId) {
      const product = this.storeProducts.find(item => item.product_id === productId);
      return product ? product.category : '';
    },

    productCategoryIcon(category) {
      const icons = { '服饰': '衣', '数码配件': '声', '箱包': '包', '家居日用': '居', '食品': '味', '美妆': '妆' };
      return icons[category] || '品';
    },

    productCategoryClass(category) {
      const classes = { '服饰': 'fashion', '数码配件': 'digital', '箱包': 'bag', '家居日用': 'home', '食品': 'food', '美妆': 'beauty' };
      return classes[category] || 'default';
    },

    skuLabel(sku) {
      if (!sku) {
        return '';
      }
      const attributes = sku.attributes || {};
      return Object.keys(attributes).map(key => `${key}：${attributes[key]}`).join(' / ') || sku.sku_id;
    },

    formatMoney(value) {
      const number = Number(value || 0);
      return Number.isFinite(number) ? number.toFixed(2) : '0.00';
    },

    displayOrderNo(value) {
      const id = String(value || '');
      if (id.startsWith('DEMO-')) return `FX-${id.slice(5)}`;
      return id;
    },

    displayReturnNo(value) {
      const id = String(value || '');
      if (id.startsWith('DEMO-RET-')) return `SR-${id.slice(9)}`;
      return id;
    },

    formatStoreDateTime(value) {
      if (!value) {
        return '时间待确认';
      }
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) {
        return String(value);
      }
      try {
        return new Intl.DateTimeFormat('zh-CN', {
          timeZone: 'Asia/Shanghai',
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          hour12: false
        }).format(parsed);
      } catch (error) {
        return parsed.toLocaleString('zh-CN', { hour12: false });
      }
    },

    formatOrderStatus(status) {
      const labels = {
        pending_payment: '待付款', paid: '已付款', processing: '待发货', demo_created: '订单已创建',
        awaiting_demo_fulfillment: '待发货', shipped: '已发货', in_transit: '运输中', delivered: '已签收',
        closed: '已完成', completed: '已完成', cancelled: '已取消', after_sales: '售后中'
      };
      return labels[status] || status || '状态待确认';
    },

    deliveryStages(status) {
      const sequence = ['awaiting_demo_fulfillment', 'shipped', 'in_transit', 'delivered'];
      const labels = ['已支付', '杭州出库', '运输中', '河南签收'];
      const normalized = status === 'demo_created' ? 'awaiting_demo_fulfillment' : status;
      let currentIndex = sequence.indexOf(normalized);
      if (normalized === 'closed') currentIndex = sequence.length - 1;
      if (normalized === 'cancelled') currentIndex = -1;
      return labels.map((label, index) => ({
        label,
        done: currentIndex >= index,
        current: currentIndex === index
      }));
    },

    // 检查系统各组件连接状态
    checkSystemStatus() {
      let username = '';
      if (this.selectedUser && this.selectedUser.length > 1) {
        username = this.selectedUser[1];
      }
      
      const statusUrl = `${this.base_url}/api/get-system-status?username=${encodeURIComponent(username)}`;
      
      fetch(statusUrl)
        .then(response => response.json())
        .then(data => {
          this.systemStatus = {
            server: data.server,
            digital_human: data.digital_human,
            remote_audio: data.remote_audio
          };
        })
        .catch(error => {
          console.warn('获取系统状态失败:', error);
          this.systemStatus = {
            server: false,
            digital_human: false,
            remote_audio: false
          };
        });
    },

    // 启动系统状态检查定时器
    startSystemStatusTimer() {
      // 立即执行一次
      this.checkSystemStatus();
      
      if (this.systemStatusTimer) {
        clearInterval(this.systemStatusTimer);
      }
      // 每3秒检查一次
      this.systemStatusTimer = setInterval(() => {
        this.checkSystemStatus();
      }, 3000);
    },

    // 同步音频配置（麦克风和扬声器开关状态）
    syncAudioConfig() {
      const url = `${this.base_url}/api/get-audio-config`;
      fetch(url)
        .then(response => response.json())
        .then(data => {
          if (data.mic !== undefined) {
            this.source_record_enabled = data.mic;
          }
          if (data.speaker !== undefined) {
            this.play_sound_enabled = data.speaker;
          }
        })
        .catch(error => {
          console.warn('同步音频配置失败:', error);
        });
    },

    // 启动音频配置同步定时器（每分钟同步一次）
    startAudioConfigSyncTimer() {
      // 立即执行一次
      this.syncAudioConfig();

      if (this.audioConfigTimer) {
        clearInterval(this.audioConfigTimer);
      }
      // 每60秒同步一次
      this.audioConfigTimer = setInterval(() => {
        this.syncAudioConfig();
      }, 60000);
    },

    async initFayService() {
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsHost = window.location.hostname;
      const wsUrl = `${wsProtocol}//${wsHost}:10003`;
      this.fayService = new FayInterface(wsUrl, this.base_url, this);
      await this.loadUserList();
      this.fayService.registerHumanOutput();
    },
    async loadUserList() {
      try {
        const result = await this.fayService.getUserList();
        if (result && result.list) {
          this.userList = result.list;
          if (this.userList.length > 0) {
            this.selectedUser = this.userList[0];
            this.fayService.registerHumanOutput(this.selectedUser[1]);
            await this.loadMessageHistory(this.selectedUser[1]);
          }
        }
      } catch (error) {
        console.error('Failed to load user list:', error);
        this.$message.error('Failed to load user list. Please try again.');
      }
    },
    async sendMessage() {
      let _this = this;
      let text = String(_this.newMessage || '').trim();
      if (this.chatStyleSending) return;
      if (!text && this.chatStylePhoto && !this.chatStylePhotoSent) text = '请看看这张照片，给我穿搭建议';
      if (!text) {
        alert('请输入内容');
        return;
      }
      if (_this.selectedUser === 'others' && !_this.othersUser) {
        alert('请输入自定义用户名');
        return;
      }
      if (_this.selectedUser !== 'others'
          && (!Array.isArray(_this.selectedUser) || !_this.selectedUser[1])) {
        this.$message.warning('客户列表尚未加载完成，请稍后再试');
        return;
      }
      if (this.liveState != 1) {
        alert('请先开启服务');
        return;
      }
      let usernameToSend = _this.selectedUser === 'others' ? _this.othersUser : _this.selectedUser[1];
      this.fayService.unlockAudio();
      this.fayService.registerHumanOutput(usernameToSend);

      this.timer = setTimeout(() => {
        let height = document.querySelector('.chatmessage').scrollHeight;
        document.querySelector('.chatmessage').scrollTop = height;
      }, 1000);
      let url = `${this.base_url}/api/send`;
      let send_data = {
        "msg": text,
        "username": usernameToSend
      };
      if (this.commerceProductContext && text === this.commerceProductContext.prompt) {
        send_data.commerce_product_id = this.commerceProductContext.productId;
      }
      this.commerceProductContext = null;

      this.chatStyleSending = true;
      try {
        await this.ensureDemoStoreSession(false);
        const attachment = this.prepareChatStyleAttachment ? await this.prepareChatStyleAttachment() : null;
        if (attachment) send_data.styling_attachment_id = attachment;
        const previous = this.syncChatStylePresentation ? await this.styleRequest('/api/styling/conversation') : null;
        if (attachment || /穿搭|搭配|搭一套|搭一身|配一套|配一身|季节|预算|保留|正式|花哨|宽松|跳过/.test(text)) {
          this.chatStylePending = true; this.chatStyleProgress = attachment ? '我正在本机看照片中的服装…' : '数字客服正在了解需求并选择合适的工具…';
          this.pollChatStylePresentation(previous && previous.revision);
        }
        const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'data=' + encodeURIComponent(JSON.stringify(send_data))});
        const data = await response.json();
        if (!response.ok || data.result !== 'successful') throw new Error(data.message || '消息发送失败');
        _this.newMessage = '';
        if (attachment) { this.chatStylePhotoSent = true; this.chatStyleAttachmentId = null; }
      } catch (error) {
        this.chatStylePending = false; this.chatStyleProgress = ''; clearTimeout(this.chatStyleTimer);
        this.$message.error(error.message || '发送失败，请重试');
      }
      finally { this.chatStyleSending = false; }
    },
    getData() {
      this.fayService.getRunStatus().then((data) => {
          if (data) {
              if(data.status){
                  this.liveState = 1;
                  this.configEditable = false;
              }else{
                  this.liveState = 0;
                  this.configEditable = true;
              }
              
          }
      });
      this.fayService.getData().then((data) => {
          if (data) {
              this.updateConfigFromData(data.config);
          }
      });
  },
  updateConfigFromData(config) {
    
      if (config.interact) {
          this.play_sound_enabled = config.interact.playSound;
      }
      if (config.source && config.source.record) {
          this.source_record_enabled = config.source.record.enabled;
      }
  },
  saveConfig() {
    let url = `${this.base_url}/api/submit`;
    let send_data = {
        "config": {
            "source": {
                "record": {
                    "enabled": this.source_record_enabled,
                },
            },
            "interact": {
                "playSound": this.play_sound_enabled,
            }
        }
    };

    return new Promise((resolve, reject) => {
      let xhr = new XMLHttpRequest();
      xhr.open("post", url);
      xhr.setRequestHeader("Content-type", "application/x-www-form-urlencoded");
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(`保存配置失败（HTTP ${xhr.status || '网络错误'}）`));
          return;
        }
        try {
          const data = JSON.parse(xhr.responseText || '{}');
          if (data.result === 'successful') {
            resolve(true);
          } else {
            reject(new Error(data.message || '保存配置失败'));
          }
        } catch (error) {
          reject(new Error('保存配置响应格式无效'));
        }
      };
      xhr.onerror = function () {
        reject(new Error('保存配置时网络连接失败'));
      };
      xhr.send('data=' + encodeURIComponent(JSON.stringify(send_data)));
    });
},
  async changeRecord(){
    const previousValue = this.source_record_enabled;
    this.source_record_enabled = !previousValue;
    try {
      if (this.linkChatVoice) await this.linkChatVoice(this.source_record_enabled);
      await this.saveConfig();
    } catch (error) {
      this.source_record_enabled = previousValue;
      if (this.linkChatVoice) await this.linkChatVoice(previousValue).catch(() => {});
      this.$message.error(error.message || '麦克风设置保存失败');
    }
  },
  async changeSound(){
    const previousValue = this.play_sound_enabled;
    this.play_sound_enabled = !previousValue;
    try {
      await this.saveConfig();
    } catch (error) {
      this.play_sound_enabled = previousValue;
      this.$message.error(error.message || '语音播放设置保存失败');
    }
  },
    loadUserList() {
      this.fayService.getUserList().then((response) => {
        if (response && response.list) {
          let list = response.list || [];
          // 始终确保 "User"（主人）存在于列表中
          const defaultUserExists = list.some(user => user[1] === 'User');
          if (!defaultUserExists) {
            // 将主人添加到列表开头
            list = [[1, 'User'], ...list];
          }
          this.userList = list;
          if (!this.selectedUser) {
            this.selectUser(this.userList[0]);
          }
        }
      });
    },
    startUserListTimer() {
      // 清除可能存在的旧定时器
      if (this.userListTimer) {
        clearInterval(this.userListTimer);
      }
      // 设置新的定时器，每30秒执行一次
      this.userListTimer = setInterval(() => {
        this.loadUserList();
      }, 30000);
    },
    showDeleteBtn(user) {
      // 只有非主人用户才显示删除按钮
      if (user[1] !== 'User') {
        this.$set(user, 'showDelete', true);
      }
    },
    hideDeleteBtn(user) {
      this.$set(user, 'showDelete', false);
    },
    confirmDeleteUser(user) {
      if (user[1] === 'User') {
        this.$message.warning('无法删除主人账户');
        return;
      }
      this.$confirm(`确定要删除用户 "${user[1]}" 吗？这将同时删除该用户的所有聊天记录和记忆数据，此操作不可恢复！`, '删除确认', {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning'
      }).then(() => {
        this.deleteUser(user);
      }).catch(() => {
        // 用户取消
      });
    },
    deleteUser(user) {
      this.fayService.fetchData(`${this.base_url}/api/delete-user`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user[1] })
      }).then((response) => {
        if (response && response.success) {
          this.$message.success(`用户 "${user[1]}" 已删除`);
          // 从列表中移除该用户
          const index = this.userList.findIndex(u => u[0] === user[0]);
          if (index > -1) {
            this.userList.splice(index, 1);
          }
          // 如果删除的是当前选中的用户，切换到主人
          if (this.selectedUser && this.selectedUser[0] === user[0]) {
            const defaultUser = this.userList.find(u => u[1] === 'User') || this.userList[0];
            if (defaultUser) {
              this.selectUser(defaultUser);
            }
          }
        } else {
          this.$message.error(response.message || '删除失败');
        }
      }).catch((err) => {
        this.$message.error('删除用户时发生错误');
        console.error(err);
      });
    },
    showAddUserDialog() {
      this.newUsername = '';
      this.addUserDialogVisible = true;
    },
    addUser() {
      const username = this.newUsername.trim();
      if (!username) {
        this.$message.warning('请输入用户名');
        return;
      }
      if (username === 'User') {
        this.$message.warning('不能使用保留的用户名 "User"');
        return;
      }
      // 检查用户名是否已存在
      if (this.userList.some(u => u[1] === username)) {
        this.$message.warning('该用户名已存在');
        return;
      }
      // 调用后端 API 添加用户
      this.fayService.fetchData(`${this.base_url}/api/add-user`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username })
      }).then((response) => {
        if (response && response.success) {
          this.$message.success(`用户 "${username}" 已添加`);
          // 添加到用户列表
          this.userList.push([response.uid, username]);
          // 选中新添加的用户
          this.selectUser([response.uid, username]);
          this.addUserDialogVisible = false;
        } else {
          this.$message.error(response.message || '添加失败');
        }
      }).catch((err) => {
        this.$message.error('添加用户时发生错误');
        console.error(err);
      });
    },
    selectUser(user) {
      // 如果点击的是当前已选中的用户，打开补充信息编辑框
      if (this.selectedUser && this.selectedUser[0] === user[0]) {
        this.openExtraInfoDialog(user);
        return;
      }
      this.selectedUser = user;
      if (this.chatStylePresentations) this.chatStylePresentations = {};
      if (this.clearChatStylePhoto) this.clearChatStylePhoto();
      this.fayService.registerHumanOutput(user[1]);
      // 切换用户时重置后台任务面板，立即拉取新用户的任务状态
      this.execStatusUsername = user[1];
      this.execStatus = { status: 'idle', original_request: '', current_step: '', steps_done: 0, elapsed: 0, error: null, progress_messages: [] };
      this.pollExecStatus();
      // 切换身份由服务器签发的新连接票据完成，禁止客户端自行重绑定。
      this.loadMessageHistory(user[1], 'common');
      this.loadStorefront(true);
    },
    // 打开用户信息编辑对话框
    openExtraInfoDialog(user) {
      this.editingUserForExtraInfo = user;
      this.editingExtraInfo = '';
      this.editingUserPortrait = '';
      this.extraInfoDialogVisible = true;
      // 并行获取补充信息和用户画像
      Promise.all([
        this.fayService.fetchData(`${this.base_url}/api/get-user-extra-info`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: user[1] })
        }),
        this.fayService.fetchData(`${this.base_url}/api/get-user-portrait`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: user[1] })
        })
      ]).then(([extraInfoRes, portraitRes]) => {
        if (extraInfoRes && extraInfoRes.success) {
          this.editingExtraInfo = extraInfoRes.extra_info || '';
        }
        if (portraitRes && portraitRes.success) {
          this.editingUserPortrait = portraitRes.user_portrait || '';
        }
      }).catch((error) => {
        console.error('获取用户信息失败:', error);
      });
    },
    // 保存用户信息（补充信息和用户画像）
    saveUserInfo() {
      if (!this.editingUserForExtraInfo) return;
      const username = this.editingUserForExtraInfo[1];
      // 并行保存补充信息和用户画像
      Promise.all([
        this.fayService.fetchData(`${this.base_url}/api/update-user-extra-info`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username, extra_info: this.editingExtraInfo })
        }),
        this.fayService.fetchData(`${this.base_url}/api/update-user-portrait`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: username, user_portrait: this.editingUserPortrait })
        })
      ]).then(([extraInfoRes, portraitRes]) => {
        if ((extraInfoRes && extraInfoRes.success) && (portraitRes && portraitRes.success)) {
          this.$notify({
            title: '成功',
            message: '用户信息已保存',
            type: 'success',
          });
          this.extraInfoDialogVisible = false;
        } else {
          this.$notify({
            title: '失败',
            message: '部分信息保存失败',
            type: 'error',
          });
        }
      }).catch((error) => {
        this.$notify({
          title: '错误',
          message: '保存用户信息时出错',
          type: 'error',
        });
      });
    },
    startLive() {
      this.liveState = 2
      this.fayService.startLive().then(() => {
        this.sendSuccessMsg('已开启！');
        this.getData();
      });
  },
  stopLive() {
      this.fayService.stopLive().then(() => {
          this.liveState = 3
          this.sendSuccessMsg('已关闭！');
      });
  },

    loadMessageHistory(username, type) {
      // 重置分页状态
      this.messageOffset = 0;
      this.hasMoreMessages = false;
      this.loadingMoreMessages = false;

      this.fayService.getMessageHistory(username, this.messageLimit, 0).then((response) => {
        if (response) {
          this.messages = response.list || [];
          this.hasMoreMessages = response.hasMore || false;
          this.messageOffset = this.messages.length;

          if(type == 'common'){
            this.$nextTick(() => {
              const chatContainer = document.querySelector('.chatmessage');
              if (chatContainer) {
                chatContainer.scrollTop = chatContainer.scrollHeight;
                // 绑定滚动事件监听
                this.bindScrollListener(chatContainer);
              }
            });
          }
        }
      });
    },

    // 绑定滚动事件监听器
    bindScrollListener(container) {
      // 移除旧的监听器
      if (this._scrollHandler) {
        container.removeEventListener('scroll', this._scrollHandler);
      }

      // 创建新的监听器
      this._scrollHandler = () => {
        // 当滚动到顶部附近时加载更多
        if (container.scrollTop < 50 && this.hasMoreMessages && !this.loadingMoreMessages) {
          this.loadMoreMessages();
        }
      };

      container.addEventListener('scroll', this._scrollHandler);
    },

    // 加载更多历史消息
    loadMoreMessages() {
      if (!this.selectedUser || this.loadingMoreMessages || !this.hasMoreMessages) {
        return;
      }

      this.loadingMoreMessages = true;
      const username = this.selectedUser[1];

      this.fayService.getMessageHistory(username, this.messageLimit, this.messageOffset).then((response) => {
        if (response && response.list && response.list.length > 0) {
          const chatContainer = document.querySelector('.chatmessage');
          const oldScrollHeight = chatContainer ? chatContainer.scrollHeight : 0;

          // 将新消息添加到列表开头（因为是更早的消息）
          this.messages = [...response.list, ...this.messages];
          this.messageOffset += response.list.length;
          this.hasMoreMessages = response.hasMore || false;

          // 保持滚动位置
          this.$nextTick(() => {
            if (chatContainer) {
              const newScrollHeight = chatContainer.scrollHeight;
              chatContainer.scrollTop = newScrollHeight - oldScrollHeight;
            }
          });
        } else {
          this.hasMoreMessages = false;
        }
        this.loadingMoreMessages = false;
      }).catch(() => {
        this.loadingMoreMessages = false;
      });
    },
    sendSuccessMsg(message) {
      this.$notify({
          title: '成功',
          message,
          type: 'success',
      });
  
} ,
adoptText(id) {
// 调用采纳接口
this.fayService.fetchData(`${this.base_url}/api/adopt-msg`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ id })
})
.then((response) => {
  if (response && response.status === 'success') {
    // 处理成功的响应
    this.$notify({
      title: '成功',
      message: response.msg,  // 显示成功消息
      type: 'success',
    });

    this.loadMessageHistory(this.selectedUser[1], 'adopt');
  } else {
    // 处理失败的响应
    this.$notify({
      title: '失败',
      message: response ? response.msg : '请求失败',
      type: 'error',
    });
  }
})
.catch((error) => {
  // 处理网络错误或HTTP错误
  this.$notify({
    title: '错误',
    message: error.message || '请求失败',
    type: 'error',
  });
});
},

// 取消采纳
unadoptText(id) {
  this.fayService.fetchData(`${this.base_url}/api/unadopt-msg`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ id })
  })
  .then((response) => {
    if (response && response.status === 'success') {
      this.$notify({
        title: '成功',
        message: response.msg,
        type: 'success',
      });

      // 更新本地消息列表中所有相关消息的采纳状态
      if (response.unadopted_ids && response.unadopted_ids.length > 0) {
        this.messages.forEach(msg => {
          if (response.unadopted_ids.includes(msg.id)) {
            msg.is_adopted = 0;
          }
        });
      }
    } else {
      this.$notify({
        title: '失败',
        message: response ? response.msg : '请求失败',
        type: 'error',
      });
    }
  })
  .catch((error) => {
    this.$notify({
      title: '错误',
      message: error.message || '请求失败',
      type: 'error',
    });
  });
}
,
  minimizeThinkPanel() {
    this.isThinkPanelMinimized = !this.isThinkPanelMinimized;
    const panel = document.querySelector('.think-panel');
    panel.classList.toggle('minimized');
  },

  // 解析消息中的 think 和 prestart 内容
  parseThinkContent(content) {
    if (!content) {
      return { thinkContent: '', mainContent: '', prestartContent: '' };
    }

    let thinkContent = '';
    let mainContent = content;
    let prestartContent = '';

    // 解析 prestart 标签 - 使用贪婪匹配确保匹配到最后一个 </prestart>
    // 同时支持多个 prestart 标签的情况，以及支持属性
    const prestartRegex = /<prestart(?:[^>]*)>([\s\S]*)<\/prestart>/i;
    const prestartMatch = mainContent.match(prestartRegex);
    if (prestartMatch && prestartMatch[1]) {
      prestartContent = this.trimThinkLines(prestartMatch[1]);
      // 移除所有 prestart 标签及其内容
      mainContent = mainContent.replace(/<prestart(?:[^>]*)>[\s\S]*<\/prestart>/gi, '');
    }

    // 先尝试匹配完整的 think 标签
    const completeRegex = /<think>([\s\S]*?)<\/think>/i;
    const completeMatch = mainContent.match(completeRegex);

    if (completeMatch && completeMatch[1]) {
      // 完整的 think 标签
      const rawThink = completeMatch[1];
      thinkContent = this.trimThinkLines(rawThink);
      mainContent = mainContent.replace(completeRegex, '').replace(/^\s+/, '').replace(/\s+$/, '');
      return { thinkContent, mainContent, prestartContent };
    }

    // 尝试匹配未完成的 think 标签（只有开始标签）
    const incompleteRegex = /<think>([\s\S]*)/i;
    const incompleteMatch = mainContent.match(incompleteRegex);

    if (incompleteMatch && incompleteMatch[1]) {
      // 未完成的 think 标签，正在接收中
      const rawThink = incompleteMatch[1];
      thinkContent = this.trimThinkLines(rawThink);
      mainContent = ''; // 正在思考中，主内容为空
      return { thinkContent, mainContent, prestartContent };
    }

    return { thinkContent: '', mainContent: mainContent.replace(/^\s+/, '').replace(/\s+$/, ''), prestartContent };
  },

  // 处理 think 内容的每行 trim
  trimThinkLines(rawThink) {
    const lines = rawThink.split(/\r?\n/);
    const trimmedLines = [];
    for (let i = 0; i < lines.length; i++) {
      const trimmed = lines[i].replace(/^\s+/, '').replace(/\s+$/, '');
      if (trimmed.length > 0) {
        trimmedLines.push(trimmed);
      }
    }
    return trimmedLines.join('\n');
  },

  // 切换 think 内容的展开/折叠状态
  toggleThink(index) {
    const message = this.messages[index];
    this.$set(message, 'thinkExpanded', !message.thinkExpanded);
  },

  // 切换 prestart 内容的展开/折叠状态
  togglePrestart(index) {
    const message = this.messages[index];
    this.$set(message, 'prestartExpanded', !message.prestartExpanded);
  },

  // 检测并转换图片路径为缩略图
  convertImagePaths(content) {
    if (!content) return content;
    // 匹配常见图片路径格式：
    // Windows: D:\path\to\image.png 或 D:/path/to/image.png
    // Unix: /path/to/image.png
    // 支持的图片格式: png, jpg, jpeg, gif, bmp, webp
    const imagePathRegex = /([A-Za-z]:[\\\/][^\s<>"']+\.(png|jpg|jpeg|gif|bmp|webp))/gi;

    const baseUrl = window.location.protocol + '//' + window.location.hostname + ':' + window.location.port;

    return content.replace(imagePathRegex, (match) => {
      // 对原始路径进行编码
      const encodedPath = encodeURIComponent(match);
      // 通过后端 API 获取图片（解决浏览器安全限制）
      const imgSrc = `${baseUrl}/api/local-image?path=${encodedPath}`;
      // 用于显示的安全路径
      const displayPath = match.replace(/\\/g, '/').replace(/'/g, '&#39;').replace(/"/g, '&quot;');
      return `<span class="image-thumbnail-container" onclick="window.openImageFile('${encodedPath}')">
        <img src="${imgSrc}" class="message-image-thumbnail" alt="图片" onerror="this.parentElement.innerHTML='<span class=\\'image-path-text\\'>${displayPath}</span>'" />
        <span class="image-zoom-hint">点击查看</span>
      </span>`;
    });
  },

  // 流式传输中的轻量渲染（仅处理换行和基本格式，避免不完整markdown导致渲染异常）
  renderStreamingText(content) {
    if (!content) return '';
    let result = content
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\\n/g, '<br>')
      .replace(/\n/g, '<br>');
    result = this.convertImagePaths(result);
    return result;
  },

  // 渲染 Markdown 内容
  renderMarkdown(content) {
    if (!content) return '';
    try {
      if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        marked.setOptions({
          breaks: true,
          gfm: true,
        });
        // 将字面量 \n 转为真正的换行符（LLM流式输出常见）
        let processed = content.replace(/\\n/g, '\n');
        // 直接交给 marked 处理，不做手动 bold/italic 替换，避免与 marked 冲突
        let result = marked.parse(processed);
        result = this.convertImagePaths(result);
        return result;
      } else {
        console.warn('[Fay] marked library not available, typeof marked =', typeof marked);
      }
    } catch (e) {
      console.error('[Fay] Markdown rendering error:', e);
    }
    // fallback
    let result = content.replace(/\\n/g, '<br>').replace(/\n/g, '<br>');
    result = this.convertImagePaths(result);
    return result;
  },

  // ===== 分享图功能 =====
  enterShareSelectMode() {
    this.shareSelectMode = true;
    this.messages.forEach(m => this.$set(m, 'shareSelected', false));
  },
  exitShareSelectMode() {
    this.shareSelectMode = false;
    this.messages.forEach(m => { m.shareSelected = false; });
  },
  toggleShareSelect(index) {
    const msg = this.messages[index];
    this.$set(msg, 'shareSelected', !msg.shareSelected);
  },
  shareSelectAll() {
    this.messages.forEach(m => this.$set(m, 'shareSelected', true));
  },
  shareDeselectAll() {
    this.messages.forEach(m => this.$set(m, 'shareSelected', false));
  },
  generateShareImage() {
    if (this.shareSelectedCount === 0) return;
    this.sharePreviewVisible = true;
  },
  downloadShareImage() {
    const container = document.getElementById('sharePreviewContainer');
    if (!container) return;
    if (typeof html2canvas !== 'function') {
      this.$message.error('html2canvas 未加载，请刷新页面后重试');
      return;
    }
    var vm = this;
    html2canvas(container, {
      scale: 2,
      useCORS: true,
      backgroundColor: '#ffffff'
    }).then(function(canvas) {
      var link = document.createElement('a');
      link.download = 'store_service_summary_' + Date.now() + '.png';
      link.href = canvas.toDataURL('image/png');
      link.click();
    }).catch(function() {
      vm.$message.error('生成图片失败，请尝试截图保存');
    });
  },

  // 检查MCP服务器状态
  checkMcpStatus() {
    const mcpUrl = `${this.base_url}/api/mcp/status`;
    
    // 使用超时设置的fetch请求
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 3000); // 3秒超时
    
    fetch(mcpUrl, { signal: controller.signal })
      .then(response => {
        clearTimeout(timeoutId);
        if (!response.ok) {
          throw new Error('MCP服务器响应不正常');
        }
        return response.json();
      })
      .then(data => {
        if (Array.isArray(data)) {
          // 检查是否有任何一个MCP服务器在线
          const hasOnlineServer = data.some(server => server.status === 'online');
          this.mcpOnlineStatus = hasOnlineServer;
        } else {
          console.warn('MCP服务器返回的数据格式不正确');
          this.mcpOnlineStatus = false;
        }
      })
      .catch(error => {
        clearTimeout(timeoutId);
        // 如果是超时错误，不输出详细错误信息
        if (error.name === 'AbortError') {
          console.warn('MCP服务器请求超时');
        } else {
          console.warn('检查MCP状态出错:', error.message);
        }
        this.mcpOnlineStatus = false;
      });
  },
  
  // 启动MCP状态检查定时器
  startMcpStatusTimer() {
    // 清除可能存在的旧定时器
    if (this.mcpCheckTimer) {
      clearInterval(this.mcpCheckTimer);
    }
    // 设置新的定时器，每30秒检查一次MCP状态
    this.mcpCheckTimer = setInterval(() => {
      this.checkMcpStatus();
    }, 30000);
  },

  // ===== 后台任务侧边栏 =====
  toggleExecPanel() {
    this.execPanelOpen = !this.execPanelOpen;
    if (this.execPanelOpen) {
      this.pollExecStatus();
    }
  },

  pollExecStatus() {
    var username = 'User';
    if (this.selectedUser && this.selectedUser.length > 1) {
      username = this.selectedUser[1];
    }
    var polledUsername = username;
    fetch(this.base_url + '/api/execution-status?username=' + encodeURIComponent(username))
      .then(function(res) { return res.json(); })
      .then(function(data) {
        // 响应返回时用户可能已切换，忽略过期响应
        var currentUsername = 'User';
        if (this.selectedUser && this.selectedUser.length > 1) {
          currentUsername = this.selectedUser[1];
        }
        if (polledUsername !== currentUsername) {
          return;
        }
        var newStatus = data.status || 'idle';
        // 后端已清理(idle)，但前端还在显示同一用户的任务状态时：
        // - 如果之前是 running，说明任务刚完成，标记为 done 并保留
        // - 如果之前已经是 done/failed/cancelled，保持显示直到新任务
        if (newStatus === 'idle'
            && (this.execStatusUsername === null || this.execStatusUsername === polledUsername)
            && this.execStatus.status && this.execStatus.status !== 'idle') {
          if (this.execStatus.status === 'running') {
            this.execStatus.status = 'done';
            this.execStatus.current_step = '执行完成';
          }
          return;
        }
        this.execStatusUsername = polledUsername;
        this.execStatus = {
          status: newStatus,
          original_request: data.original_request || '',
          current_step: data.current_step || '',
          steps_done: data.steps_done || 0,
          elapsed: data.elapsed || 0,
          error: data.error || null,
          progress_messages: data.progress_messages || []
        };
        // 有任务在跑时自动展开面板
        if (newStatus === 'running' && !this.execPanelOpen) {
          this.execPanelOpen = true;
        }
      }.bind(this))
      .catch(function() {});
  },

  startExecPollTimer() {
    this.pollExecStatus();
    if (this.execPollTimer) {
      clearInterval(this.execPollTimer);
    }
    this.execPollTimer = setInterval(function() {
      this.pollExecStatus();
    }.bind(this), 3000);
  },

  cancelExecution() {
    var username = 'User';
    if (this.selectedUser && this.selectedUser.length > 1) {
      username = this.selectedUser[1];
    }
    fetch(this.base_url + '/api/execution-cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
      if (data.success) {
        this.pollExecStatus();
      }
    }.bind(this))
    .catch(function() {});
  },

  }
});
