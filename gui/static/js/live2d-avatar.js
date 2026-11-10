(function () {
  'use strict';

  var MOUTH_OPENNESS = {
    sil: 0.0,
    PP: 0.12,
    FF: 0.32,
    TH: 0.42,
    DD: 0.46,
    kk: 0.58,
    CH: 0.58,
    SS: 0.34,
    nn: 0.44,
    RR: 0.56,
    aa: 1.0,
    E: 0.72,
    ih: 0.54,
    oh: 0.88,
    ou: 0.76
  };

  var DEFAULT_VIEW_PROFILE = {
    zoom: 1,
    x: 0,
    y: 0.035,
    compactZoom: 1
  };

  var MODEL_VIEW_PROFILES = {
    // Belfast is a full-body illustration with a large decorative base. A
    // customer-service framing keeps the face large enough for expressions
    // and lip-sync to remain readable in the narrow sidebar.
    beierfasite_2: {
      zoom: 1.92,
      x: 0,
      y: 0.165,
      compactZoom: 1.65
    }
  };

  var MOTION_MAP = {
    nod: 'touch_head',
    wave: 'main_3',
    invite: 'main_1',
    wait: 'idle',
    remind: 'mission',
    warn: 'mission',
    reject: 'touch_special',
    think: 'main_2',
    question: 'touch_special',
    explain: 'main_1',
    recommend: 'main_3',
    summary: 'complete',
    thanks: 'complete',
    apology: 'main_2',
    care: 'main_1',
    celebrate: 'mission_complete',
    surprise: 'touch_special',
    sad: 'main_2'
  };

  var STATE_MOTION_MAP = {
    idle: 'idle',
    listening: 'touch_head',
    thinking: 'main_2',
    speaking: 'main_1'
  };

  var STATE_LABELS = {
    loading: '形象加载中',
    idle: '在线 · 待机',
    listening: '正在聆听',
    thinking: '正在思考',
    speaking: '正在说话',
    offline: '等待客服连接',
    error: '形象加载失败'
  };

  function FayLive2DAvatar(options) {
    this.options = options || {};
    this.stage = document.getElementById(this.options.stageId || 'fayAvatarStage');
    this.canvasHost = document.getElementById(this.options.canvasId || 'fayLive2DCanvas');
    this.statusNode = document.getElementById(this.options.statusId || 'fayAvatarStatus');
    this.captionNode = document.getElementById(this.options.captionId || 'fayAvatarCaption');
    this.modelName = this.options.modelName || 'xiaoru_1';
    this.modelBasePath = this.options.modelBasePath || '/static/live2d/model/';
    this.model = null;
    this.app = null;
    this.loader = null;
    this.resizeObserver = null;
    this.pointerHost = null;
    this.pointerMoveHandler = null;
    this.pointerLeaveHandler = null;
    this.pointerClickHandler = null;
    this.windowResizeHandler = null;
    this.tickerHandler = null;
    this.speechQueue = [];
    this.activeSpeech = false;
    this.activeSpeechData = null;
    this.lipTimer = null;
    this.fallbackMouthTimer = null;
    this.stateResetTimer = null;
    this.actionResetTimer = null;
    this.currentMouth = 0;
    this.pointerX = 0;
    this.pointerY = 0;
    this.loaded = false;
    this.connected = false;
    this.mouthParameterAvailable = false;
    this.mouthParameterId = null;
    this.state = 'loading';
    this.currentCaption = '';
    this.currentAffect = 'neutral';
    this.currentAction = 'neutral';
  }

  FayLive2DAvatar.prototype.init = function () {
    if (!this.stage || !this.canvasHost) {
      return;
    }

    this.setState('loading');

    if (
      typeof PIXI === 'undefined' ||
      typeof Live2DCubismCore === 'undefined' ||
      typeof LIVE2DCUBISMFRAMEWORK === 'undefined' ||
      typeof LIVE2DCUBISMPIXI === 'undefined' ||
      typeof L2D === 'undefined'
    ) {
      this.fail('Live2D 运行库未加载');
      return;
    }

    try {
      this.app = new PIXI.Application(640, 760, {
        transparent: true,
        antialias: true,
        autoStart: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2)
      });
      this.app.view.className = 'fay-live2d-canvas';
      this.canvasHost.appendChild(this.app.view);

      var self = this;
      this.tickerHandler = function (deltaTime) {
        if (!self.model) {
          return;
        }
        self.updateModel(deltaTime);
        self.model.masks.update(self.app.renderer);
      };
      this.app.ticker.add(this.tickerHandler);

      this.bindPointerTracking();
      this.bindResize();
      this.loader = new L2D(this.modelBasePath);
      this.loader.load(this.modelName, this);
    } catch (error) {
      this.fail(error && error.message ? error.message : 'Live2D 初始化失败');
    }
  };

  FayLive2DAvatar.prototype.changeCanvas = function (model) {
    if (!model || !this.app) {
      this.fail('模型文件无法读取');
      return;
    }

    this.refreshDomReferences();
    this.app.stage.removeChildren();
    this.model = model;
    this.model.motions = this.model.motions || new Map();
    this.model.animator.addLayer(
      'base',
      LIVE2DCUBISMFRAMEWORK.BuiltinAnimationBlenders.OVERRIDE,
      1
    );
    this.app.stage.addChild(this.model);
    this.app.stage.addChild(this.model.masks);

    var parameterIds = Array.prototype.slice.call(this.model._coreModel.parameters.ids || []);
    var mouthCandidates = [
      'ParamMouthOpenY',
      'PARAM_MOUTH_OPEN_Y',
      'PARAM_Mouth',
      'PARAM_mouth'
    ];
    this.mouthParameterId = mouthCandidates.find(function (candidate) {
      return parameterIds.indexOf(candidate) >= 0;
    }) || parameterIds.find(function (parameterId) {
      return /mouth.*open|open.*mouth/i.test(parameterId);
    }) || null;
    this.mouthParameterAvailable = Boolean(this.mouthParameterId);
    this.loaded = true;
    this.stage.setAttribute('data-loaded', 'true');
    this.stage.setAttribute('data-mouth', this.mouthParameterId || 'unavailable');
    this.resize();
    this.startMotion('idle');
    if (this.connected) {
      this.setState('idle');
      this.setCaption('你好，我是小如。');
    } else {
      this.setState('offline');
      this.setCaption('正在连接客服服务…');
    }
    console.log(
      '[FayLive2D] model ready:',
      this.modelName,
      'mouth:',
      this.mouthParameterId || 'unavailable',
      'stage connected:',
      this.stage.isConnected,
      'stage current:',
      document.getElementById(this.options.stageId || 'fayAvatarStage') === this.stage
    );
  };

  FayLive2DAvatar.prototype.updateModel = function (delta) {
    if (!this.stage.isConnected || document.getElementById(this.options.stageId || 'fayAvatarStage') !== this.stage) {
      this.refreshDomReferences();
    }
    var model = this.model;
    var deltaTime = 0.016 * delta;

    if (!model.animator.isPlaying) {
      this.startMotion('idle');
    }
    model._animator.updateAndEvaluate(deltaTime);

    if (model._physicsRig) {
      model._physicsRig.updateAndEvaluate(deltaTime);
    }

    model.addParameterValueById('ParamAngleX', this.pointerX * 12);
    model.addParameterValueById('ParamAngleY', -this.pointerY * 8);
    model.addParameterValueById('ParamBodyAngleX', this.pointerX * 4);
    model.addParameterValueById('ParamEyeBallX', this.pointerX * 0.5);
    model.addParameterValueById('ParamEyeBallY', -this.pointerY * 0.35);

    var mouthIndex = this.mouthParameterId ?
      model._coreModel.parameters.ids.indexOf(this.mouthParameterId) : -1;
    if (mouthIndex >= 0) {
      model._coreModel.parameters.values[mouthIndex] = this.currentMouth;
    }

    model._coreModel.update();

    var shouldSort = false;
    for (var index = 0; index < model._meshes.length; index += 1) {
      var mesh = model._meshes[index];
      mesh.alpha = model._coreModel.drawables.opacities[index];
      mesh.visible = Live2DCubismCore.Utils.hasIsVisibleBit(
        model._coreModel.drawables.dynamicFlags[index]
      );
      if (
        Live2DCubismCore.Utils.hasVertexPositionsDidChangeBit(
          model._coreModel.drawables.dynamicFlags[index]
        )
      ) {
        mesh.vertices = model._coreModel.drawables.vertexPositions[index];
        mesh.dirtyVertex = true;
      }
      if (
        Live2DCubismCore.Utils.hasRenderOrderDidChangeBit(
          model._coreModel.drawables.dynamicFlags[index]
        )
      ) {
        shouldSort = true;
      }
    }

    if (shouldSort) {
      model.children.sort(function (left, right) {
        var leftIndex = model._meshes.indexOf(left);
        var rightIndex = model._meshes.indexOf(right);
        return (
          model._coreModel.drawables.renderOrders[leftIndex] -
          model._coreModel.drawables.renderOrders[rightIndex]
        );
      });
    }
    model._coreModel.drawables.resetDynamicFlags();
  };

  FayLive2DAvatar.prototype.bindPointerTracking = function () {
    var self = this;
    this.unbindPointerTracking();
    this.pointerHost = this.canvasHost;
    this.pointerMoveHandler = function (event) {
      var rect = self.canvasHost.getBoundingClientRect();
      self.pointerX = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width - 0.5) * 2));
      self.pointerY = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height - 0.5) * 2));
    };
    this.pointerLeaveHandler = function () {
      self.pointerX = 0;
      self.pointerY = 0;
    };
    this.pointerClickHandler = function () {
      self.startMotion('touch_head');
    };
    this.pointerHost.addEventListener('mousemove', this.pointerMoveHandler);
    this.pointerHost.addEventListener('mouseleave', this.pointerLeaveHandler);
    this.pointerHost.addEventListener('click', this.pointerClickHandler);
  };

  FayLive2DAvatar.prototype.unbindPointerTracking = function () {
    if (!this.pointerHost) {
      return;
    }
    this.pointerHost.removeEventListener('mousemove', this.pointerMoveHandler);
    this.pointerHost.removeEventListener('mouseleave', this.pointerLeaveHandler);
    this.pointerHost.removeEventListener('click', this.pointerClickHandler);
    this.pointerHost = null;
  };

  FayLive2DAvatar.prototype.bindResize = function () {
    var self = this;
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(function () {
        self.resize();
      });
      this.resizeObserver.observe(this.canvasHost);
    } else {
      if (this.windowResizeHandler) {
        window.removeEventListener('resize', this.windowResizeHandler);
      }
      this.windowResizeHandler = function () {
        self.resize();
      };
      window.addEventListener('resize', this.windowResizeHandler);
    }
  };

  FayLive2DAvatar.prototype.refreshDomReferences = function () {
    var currentStage = document.getElementById(this.options.stageId || 'fayAvatarStage');
    var currentHost = document.getElementById(this.options.canvasId || 'fayLive2DCanvas');
    if (!currentStage || !currentHost) {
      return false;
    }

    var hostChanged = currentHost !== this.canvasHost;
    this.stage = currentStage;
    this.statusNode = document.getElementById(this.options.statusId || 'fayAvatarStatus');
    this.captionNode = document.getElementById(this.options.captionId || 'fayAvatarCaption');

    if (hostChanged) {
      this.canvasHost = currentHost;
      if (this.app && this.app.view) {
        Array.prototype.slice.call(currentHost.querySelectorAll('canvas')).forEach(function (canvas) {
          if (canvas !== this.app.view) {
            canvas.remove();
          }
        }, this);
        currentHost.appendChild(this.app.view);
      }
      this.bindPointerTracking();
      this.bindResize();
      this.resize();
    }

    if (this.loaded) {
      this.stage.setAttribute('data-loaded', 'true');
      this.stage.setAttribute('data-mouth', this.mouthParameterId || 'unavailable');
      this.stage.setAttribute('data-affect', this.currentAffect);
      this.stage.setAttribute('data-action', this.currentAction);
      this.setState(this.state);
      if (this.captionNode && this.currentCaption) {
        this.captionNode.textContent = this.currentCaption;
      }
    }
    return true;
  };

  FayLive2DAvatar.prototype.resize = function () {
    if (!this.app || !this.canvasHost) {
      return;
    }
    var rect = this.canvasHost.getBoundingClientRect();
    var width = Math.max(1, Math.floor(rect.width || 420));
    var height = Math.max(1, Math.floor(rect.height || 640));
    this.app.renderer.resize(width, height);
    this.app.view.style.width = width + 'px';
    this.app.view.style.height = height + 'px';

    if (!this.model) {
      return;
    }

    this.model.scale.set(1, 1);
    this.model.pivot.set(0, 0);
    var bounds = this.model.getLocalBounds();
    var scaleX = (width * 0.86) / Math.max(bounds.width, 1);
    var scaleY = (height * 0.9) / Math.max(bounds.height, 1);
    var profile = MODEL_VIEW_PROFILES[this.modelName] || DEFAULT_VIEW_PROFILE;
    var zoom = Number(profile.zoom) || 1;
    if (height < 460) {
      zoom = Math.min(zoom, Number(profile.compactZoom) || zoom);
    }
    var scale = Math.min(scaleX, scaleY) * zoom;
    this.model.scale.set(scale, scale);
    this.model.pivot.set(
      bounds.x + bounds.width / 2,
      bounds.y + bounds.height / 2
    );
    this.model.position.set(
      width / 2 + width * (Number(profile.x) || 0),
      height / 2 + height * (Number(profile.y) || 0)
    );
    this.model.masks.resize(width, height);
  };

  FayLive2DAvatar.prototype.handleConnection = function (connected) {
    this.connected = connected;
    clearTimeout(this.stateResetTimer);
    this.stateResetTimer = null;
    if (!this.loaded) {
      return;
    }
    if (connected) {
      if (this.activeSpeech) {
        this.setState('speaking');
      } else {
        this.setStateWithMotion('idle');
        this.setCaption('你好，我是小如。');
      }
    } else {
      this.setState('offline');
      this.setCaption('正在连接客服服务…');
    }
  };

  FayLive2DAvatar.prototype.handleHumanMessage = function (message) {
    if (!message || message.Topic !== 'human' || !message.Data) {
      return;
    }
    var data = message.Data;
    if (data.Key === 'audio') {
      if (data.HttpValue || data.Value) {
        this.enqueueSpeech(data);
      }
      return;
    }
    if (data.Key === 'question') {
      this.setStateWithMotion('listening');
      this.setCaption(data.Value ? '你说：' + this.truncateCaption(data.Value) : '我在听。');
      return;
    }
    if (data.Key === 'log') {
      var logText = String(data.Value || '');
      if (logText.indexOf('思考') >= 0) {
        this.setStateWithMotion('thinking');
        this.setCaption('让我想一想…');
      } else if (logText.indexOf('待唤醒') >= 0) {
        this.setStateWithMotion('idle');
        this.setCaption('说出唤醒词就可以叫我。');
      } else if (logText.indexOf('聆听') >= 0 || logText.indexOf('唤醒') >= 0) {
        this.setStateWithMotion('listening');
        this.setCaption('我在听。');
      }
    }
  };

  FayLive2DAvatar.prototype.enqueueSpeech = function (data) {
    this.speechQueue.push(data);
    this.processSpeechQueue();
  };

  FayLive2DAvatar.prototype.processSpeechQueue = function () {
    if (this.activeSpeech || this.speechQueue.length === 0) {
      return;
    }
    var data = this.speechQueue.shift();
    clearTimeout(this.stateResetTimer);
    this.stateResetTimer = null;
    this.activeSpeech = true;
    this.activeSpeechData = data;
    this.startMotion(STATE_MOTION_MAP.speaking);
    this.applyAction(data.Action || null);
    this.setAffect(data.Action && data.Action.affect ? data.Action.affect : 'neutral', data.Sentiment);
    this.setCaption(data.Text || '正在说话…');
    this.setState(this.connected ? 'speaking' : 'offline');

    var lips = Array.isArray(data.Lips) ? data.Lips : [];
    if (lips.length > 0) {
      this.playLipSequence(lips, 0, data);
    } else {
      this.playFallbackMouth(data);
    }
  };

  FayLive2DAvatar.prototype.playLipSequence = function (lips, index, data) {
    var self = this;
    if (index >= lips.length) {
      this.currentMouth = 0;
      this.finishSpeech(data);
      return;
    }
    var lip = lips[index] || {};
    var viseme = String(lip.Lip || '');
    var rawOpenness = MOUTH_OPENNESS[viseme] != null ? MOUTH_OPENNESS[viseme] : 0.4;
    this.currentMouth = viseme === 'sil' ? 0 : Math.min(1, 0.18 + rawOpenness * 1.05);
    var duration = Math.max(33, Number(lip.Time) || 66);
    clearTimeout(this.lipTimer);
    this.lipTimer = setTimeout(function () {
      self.playLipSequence(lips, index + 1, data);
    }, duration);
  };

  FayLive2DAvatar.prototype.playFallbackMouth = function (data) {
    var self = this;
    var duration = Math.max(500, (Number(data.Time) || 2.2) * 1000);
    var open = false;
    clearInterval(this.fallbackMouthTimer);
    this.fallbackMouthTimer = setInterval(function () {
      open = !open;
      self.currentMouth = open ? 0.82 : 0.04;
    }, 115);
    clearTimeout(this.lipTimer);
    this.lipTimer = setTimeout(function () {
      clearInterval(self.fallbackMouthTimer);
      self.fallbackMouthTimer = null;
      self.currentMouth = 0;
      self.finishSpeech(data);
    }, duration);
  };

  FayLive2DAvatar.prototype.finishSpeech = function (data) {
    var self = this;
    if (this.activeSpeechData && data && this.activeSpeechData !== data) {
      return;
    }
    this.activeSpeech = false;
    this.activeSpeechData = null;
    this.currentMouth = 0;
    if (this.speechQueue.length > 0) {
      this.processSpeechQueue();
      return;
    }
    clearTimeout(this.stateResetTimer);
    this.stateResetTimer = setTimeout(function () {
      self.setState(self.connected ? 'idle' : 'offline');
      if (self.connected && data && data.IsEnd) {
        self.setCaption('我在听。');
      } else if (!self.connected) {
        self.setCaption('正在连接客服服务…');
      }
    }, 320);
  };

  FayLive2DAvatar.prototype.finishActiveSpeech = function (data) {
    if (!this.activeSpeech || (data && this.activeSpeechData !== data)) {
      return false;
    }
    clearTimeout(this.lipTimer);
    clearInterval(this.fallbackMouthTimer);
    this.lipTimer = null;
    this.fallbackMouthTimer = null;
    this.currentMouth = 0;
    this.finishSpeech(this.activeSpeechData || data || {});
    return true;
  };

  FayLive2DAvatar.prototype.applyAction = function (action) {
    if (!action) {
      return false;
    }
    var behavior = action.behavior || '';
    var motion = MOTION_MAP[behavior] || 'main_1';
    var started = this.startMotion(motion);
    if (this.stage) {
      this.currentAction = behavior || 'neutral';
      this.stage.setAttribute('data-action', behavior || 'neutral');
      this.stage.style.setProperty('--avatar-intensity', String(Number(action.intensity) || 0.45));
      clearTimeout(this.actionResetTimer);
      var self = this;
      this.actionResetTimer = setTimeout(function () {
        self.currentAction = 'neutral';
        self.stage.setAttribute('data-action', 'neutral');
      }, 1400);
    }
    return started;
  };

  FayLive2DAvatar.prototype.startMotion = function (motionName) {
    if (!this.model || !this.model.motions) {
      return false;
    }
    var motion = this.model.motions.get(motionName);
    var layer = this.model.animator.getLayer('base');
    if (!motion || !layer) {
      return false;
    }
    layer.play(motion);
    return true;
  };

  FayLive2DAvatar.prototype.setState = function (state) {
    this.state = state;
    if (this.stage) {
      this.stage.setAttribute('data-state', state);
    }
    if (this.statusNode) {
      this.statusNode.textContent = STATE_LABELS[state] || state;
    }
  };

  FayLive2DAvatar.prototype.setStateWithMotion = function (state) {
    this.setState(state);
    var motion = STATE_MOTION_MAP[state];
    if (motion) {
      this.startMotion(motion);
    }
  };

  FayLive2DAvatar.prototype.setAffect = function (affect, sentiment) {
    this.currentAffect = affect || 'neutral';
    if (!this.stage) {
      return;
    }
    this.stage.setAttribute('data-affect', this.currentAffect);
    var sentimentValue = Math.max(-1, Math.min(1, Number(sentiment) || 0));
    this.stage.style.setProperty('--avatar-sentiment', String(sentimentValue));
  };

  FayLive2DAvatar.prototype.setCaption = function (text) {
    this.currentCaption = text || '我在听。';
    if (!this.captionNode) {
      return;
    }
    this.captionNode.textContent = this.currentCaption;
  };

  FayLive2DAvatar.prototype.truncateCaption = function (text) {
    var normalized = String(text || '').replace(/\s+/g, ' ').trim();
    return normalized.length > 34 ? normalized.slice(0, 34) + '…' : normalized;
  };

  FayLive2DAvatar.prototype.fail = function (reason) {
    this.loaded = false;
    if (this.stage) {
      this.stage.setAttribute('data-loaded', 'false');
    }
    this.setState('error');
    this.setCaption(reason || '请检查 Live2D 模型资源');
    console.error('[FayLive2D]', reason);
  };

  FayLive2DAvatar.prototype.getDiagnostics = function () {
    return {
      loaded: this.loaded,
      model: this.modelName,
      mouthParameterAvailable: this.mouthParameterAvailable,
      mouthParameterId: this.mouthParameterId,
      currentMouth: this.currentMouth,
      activeSpeech: this.activeSpeech,
      state: this.state,
      queuedSpeech: this.speechQueue.length,
      renderer: this.app ? this.app.renderer.type : null
    };
  };

  FayLive2DAvatar.prototype.destroy = function () {
    clearTimeout(this.lipTimer);
    clearTimeout(this.stateResetTimer);
    clearTimeout(this.actionResetTimer);
    clearInterval(this.fallbackMouthTimer);
    this.speechQueue = [];
    this.activeSpeech = false;
    this.activeSpeechData = null;
    this.unbindPointerTracking();

    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    if (this.windowResizeHandler) {
      window.removeEventListener('resize', this.windowResizeHandler);
      this.windowResizeHandler = null;
    }
    if (this.app) {
      if (this.tickerHandler) {
        this.app.ticker.remove(this.tickerHandler);
      }
      this.app.destroy(true);
    }
    this.app = null;
    this.model = null;
    this.loaded = false;
    if (window.fayAvatar === this) {
      window.fayAvatar = null;
    }
  };

  function initializeAvatar() {
    if (window.fayAvatar) {
      return;
    }
    var stage = document.getElementById('fayAvatarStage');
    if (!stage) {
      return;
    }
    window.fayAvatar = new FayLive2DAvatar({
      stageId: 'fayAvatarStage',
      canvasId: 'fayLive2DCanvas',
      statusId: 'fayAvatarStatus',
      captionId: 'fayAvatarCaption',
      modelName: stage.getAttribute('data-model') || 'xiaoru_1',
      modelBasePath: stage.getAttribute('data-model-base') || '/static/live2d/model/'
    });
    window.fayAvatar.init();
  }

  window.initializeFayLive2D = initializeAvatar;

  function initializeWithoutVue() {
    var vueRoot = document.getElementById('app');
    if (!vueRoot || !vueRoot.__vue__) {
      initializeAvatar();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeWithoutVue, { once: true });
  } else {
    initializeWithoutVue();
  }
})();
