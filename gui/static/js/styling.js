(function () {
  'use strict';
  window.FayStylingMixin = {
    data() {
      return {
        styleMode: 'person', stylePhoto: null, stylePreview: '', styleConsent: false,
        styleSavingConsent: false, styleBusy: false, styleJob: null, styleFeedback: '',
        styleStatus: {}, styleTimer: null, styleRequestGeneration: 0, stylePolling: false, styleKeepIds: [],
        styleCompareIds: [], styleComparison: null, styleInitialized: false, styleLoading: false, styleDragging: false, styleSyncTimer: null,
        styleOptions: { scene: '日常', style: '简约', budget: 300, fit: '宽松', gender: '不限',
          colors: [], excluded_colors: [], height_cm: null, weight_kg: null, temperature: null },
        styleScenes: ['日常', '通勤', '面试', '约会', '旅行', '运动'],
        chatStylePhoto: null, chatStylePreview: '', chatStyleConsent: false, chatStyleMode: 'person',
        chatStylePhotoSent: false, chatStyleAttachmentId: null, chatStyleSending: false,
        chatStylePending: false, chatStyleProgress: '', chatStyleTimer: null, chatStylePresentations: {},
        chatStyleDragging: false, chatVoiceTimer: null, chatVoiceUser: null,
        styleStyles: ['休闲', '简约', '通勤', '运动', '复古', '街头'],
        styleColors: ['黑', '白', '灰', '蓝', '棕', '米', '绿', '红', '粉', '紫', '黄'],
        styleCategoryLabels: { tops: '上衣', bottoms: '裤装', outerwear: '外套', 'one-pieces': '裙装', shoes: '鞋', accessory: '配饰' }
      };
    },
    computed: {
      styleResult() { return this.styleJob && this.styleJob.result; }
    },
    watch: {
      source_record_enabled(value) {
        if (Array.isArray(this.selectedUser)) this.linkChatVoice(!!value).catch(error => this.$message.warning(error.message));
      },
      selectedUser() {
        if (this.source_record_enabled) this.linkChatVoice(true).catch(error => this.$message.warning(error.message));
      }
    },
    beforeDestroy() {
      this.styleRequestGeneration++;
      clearTimeout(this.styleTimer);
      clearTimeout(this.styleSyncTimer);
      clearTimeout(this.chatStyleTimer);
      clearTimeout(this.chatVoiceTimer);
      if (this.chatVoiceUser) this.styleRequest('/api/styling/voice', 'POST', {username: this.chatVoiceUser, enabled: false}).catch(() => {});
      if (this.chatStylePreview) URL.revokeObjectURL(this.chatStylePreview);
      if (this.stylePreview) URL.revokeObjectURL(this.stylePreview);
    },
    methods: {
      chooseChatStylePhoto() { if (!this.chatStyleSending) this.$refs.chatStylePhotoInput.click(); },
      handleChatStylePhoto(event) {
        const file = event.target.files[0]; if (file) this.setChatStylePhoto(file); event.target.value = '';
      },
      handleChatStyleDrop(event) {
        this.chatStyleDragging = false;
        const files = Array.from(event.dataTransfer.files || []);
        if (files.length !== 1) { this.$message.warning('一次上传一张本人照或衣服照'); return; }
        this.setChatStylePhoto(files[0]);
      },
      setChatStylePhoto(file) {
        if (this.chatStyleSending || this.chatStylePending) { this.$message.info('请等这次搭配回复完成再换照片'); return; }
        if (!/^image\/(jpeg|png|webp)$/.test(file.type) || !file.size || file.size > 64 * 1024 * 1024) {
          this.$message.error('请选择 JPEG / PNG / WebP 图片，最大64MB'); return;
        }
        this.clearChatStylePhoto();
        this.chatStylePhoto = file; this.chatStylePreview = URL.createObjectURL(file);
        this.mobileWorkspaceTab = 'service';
      },
      clearChatStylePhoto() {
        if (this.chatStyleSending) return;
        if (this.chatStyleAttachmentId && !this.chatStylePhotoSent) this.styleRequest('/api/styling/jobs/' + this.chatStyleAttachmentId, 'DELETE').catch(() => {});
        if (this.chatStylePreview) URL.revokeObjectURL(this.chatStylePreview);
        this.chatStylePhoto = null; this.chatStylePreview = ''; this.chatStyleConsent = false;
        this.chatStylePhotoSent = false; this.chatStyleAttachmentId = null;
      },
      async prepareChatStyleAttachment() {
        if (!this.chatStylePhoto || this.chatStylePhotoSent) return null;
        if (!this.chatStyleConsent) throw new Error('请勾选同意本机处理这张照片');
        if (this.chatStyleAttachmentId) return this.chatStyleAttachmentId;
        const body = new FormData(); body.append('photo', this.chatStylePhoto);
        body.append('mode', this.chatStyleMode); body.append('consent', 'true');
        const job = await this.styleRequest('/api/styling/attachments', 'POST', body);
        this.chatStyleAttachmentId = job.job_id;
        return job.job_id;
      },
      async syncChatStylePresentation(revision) {
        try {
          const presentation = await this.styleRequest('/api/styling/conversation');
          if (!presentation || (revision && presentation.revision !== revision)) return;
          if (!presentation.conversation_id) return;
          if (presentation.username && Array.isArray(this.selectedUser) && presentation.username !== this.selectedUser[1]) return;
          if (presentation.conversation_id) this.$set(this.chatStylePresentations, presentation.conversation_id, presentation);
          (this.messages || []).forEach(message => this.attachChatStylePresentation(message));
          if (this.$nextTick) this.$nextTick(() => {
            const chat = this.$el.querySelector('.chatmessage');
            if (chat) chat.scrollTop = chat.scrollHeight;
          });
          this.chatStylePending = false; this.chatStyleProgress = '';
          clearTimeout(this.chatStyleTimer);
          if (this.styleInitialized) this.scheduleStyleSync();
        } catch (_) { /* Keep chat usable while the tool endpoint reconnects. */ }
      },
      attachChatStylePresentation(message) {
        if (message.type !== 'fay') return;
        const presentation = this.chatStylePresentations[message.conversation_id];
        if (presentation && (!this.selectedUser || message.username === this.selectedUser[1])) this.$set(message, 'presentation', presentation);
      },
      pollChatStylePresentation(previousRevision, attempt) {
        clearTimeout(this.chatStyleTimer);
        this.chatStyleTimer = setTimeout(async () => {
          try {
            const current = await this.styleRequest('/api/styling/conversation');
            if (current && current.conversation_id && current.revision !== previousRevision) { await this.syncChatStylePresentation(current.revision); return; }
          } catch (_) {}
          if ((attempt || 0) < 180) this.pollChatStylePresentation(previousRevision, (attempt || 0) + 1);
          else { this.chatStylePending = false; this.chatStyleProgress = ''; this.$message.info('搭配回复等待超时，请检查服务日志后重试'); }
        }, 2000);
      },
      async linkChatVoice(enabled) {
        clearTimeout(this.chatVoiceTimer);
        const username = Array.isArray(this.selectedUser) ? this.selectedUser[1] : this.othersUser;
        if (this.chatVoiceUser && (this.chatVoiceUser !== username || !enabled)) {
          await this.styleRequest('/api/styling/voice', 'POST', {username: this.chatVoiceUser, enabled: false});
          this.chatVoiceUser = null;
        }
        if (!enabled || !username) return;
        await this.styleRequest('/api/styling/voice', 'POST', {username, enabled: true});
        this.chatVoiceUser = username;
        this.chatVoiceTimer = setTimeout(() => {
          if (this.source_record_enabled) this.linkChatVoice(true).catch(error => this.$message.warning(error.message));
        }, 25000);
      },
      chatStyleSize(card, plan) {
        const options = (plan || {}).options || {};
        let prompt = '这款商品 ' + card.product_id + ' 适合我穿什么尺码？';
        if (options.height_cm) prompt += '身高' + options.height_cm + 'cm，';
        if (options.weight_kg) prompt += '体重' + options.weight_kg + 'kg，';
        if (options.fit) prompt += options.fit;
        this.sendCommercePrompt(prompt);
      },
      async tryChatStyleProduct(card) {
        const oldMode = this.styleMode, oldPhoto = this.stylePhoto;
        if (this.chatStylePhoto && this.chatStyleMode === 'person') { this.styleMode = 'person'; this.stylePhoto = this.chatStylePhoto; }
        try { await this.tryStyleProduct(card); }
        finally { this.styleMode = oldMode; this.stylePhoto = oldPhoto; }
      },
      async styleRequest(path, method, body) {
        const token = await this.ensureDemoStoreSession(false);
        const form = typeof FormData !== 'undefined' && body instanceof FormData;
        const options = { method: method || 'GET', credentials: 'same-origin',
          headers: { Accept: 'application/json', 'X-Fay-Demo-CSRF': token } };
        if (body) {
          if (!form) options.headers['Content-Type'] = 'application/json';
          options.body = form ? body : JSON.stringify(body);
        }
        const response = await fetch(this.base_url + path, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || '穿搭服务暂不可用');
        return data.data;
      },
      async loadStyleAdvisor() {
        if (this.styleLoading) return;
        this.styleLoading = true;
        try {
          await this.ensureDemoStoreSession(false);
          const values = await Promise.all([this.styleRequest('/api/styling/status'), this.styleRequest('/api/styling/preferences'), this.styleRequest('/api/styling/latest')]);
          this.styleStatus = values[0];
          if (!this.styleInitialized) this.styleOptions = Object.assign({}, this.styleOptions, values[1]);
          this.styleInitialized = true;
          if (!this.styleBusy && values[2] && (!this.styleJob || values[2].job_id !== this.styleJob.job_id)) {
            this.styleJob = values[2]; this.styleKeepIds = [];
            this.styleOptions = Object.assign({}, this.styleOptions, this.styleJob.result.options);
          }
          else if (!this.styleBusy && values[2] && this.styleJob && values[2].job_id === this.styleJob.job_id) {
            this.styleJob = values[2];
            this.styleOptions = Object.assign({}, this.styleOptions, this.styleJob.result.options);
          }
        } catch (error) { this.styleStatus = { available: false, message: error.message }; }
        finally { this.styleLoading = false; }
      },
      scheduleStyleSync() {
        clearTimeout(this.styleSyncTimer);
        this.styleSyncTimer = setTimeout(async () => {
          if (this.styleBusy) return;
          try {
            const job = await this.styleRequest('/api/styling/latest');
            if (job && !this.styleBusy) {
              this.styleJob = job;
              this.styleOptions = Object.assign({}, this.styleOptions, job.result.options);
            }
          } catch (_) { /* A chat reply should not show a second network error. */ }
        }, 700);
      },
      chooseStylePhoto() { if (!this.styleBusy) this.$refs.stylePhotoInput.click(); },
      handleStylePhoto(event) { const file = event.target.files[0]; if (file) this.setStylePhoto(file); event.target.value = ''; },
      handleStyleDrop(event) {
        this.styleDragging = false;
        if (this.styleBusy) return;
        const files = Array.from(event.dataTransfer.files || []);
        if (files.length !== 1) { this.$message.warning('一次拖入一张照片'); return; }
        this.setStylePhoto(files[0]);
      },
      setStylePhoto(file) {
        if (this.styleBusy) return;
        if (!/^image\/(jpeg|png|webp)$/.test(file.type) || !file.size || file.size > 64 * 1024 * 1024) {
          this.$message.error('请选择非空的 JPEG / PNG / WebP 图片，最大64MB'); return;
        }
        if (this.stylePreview) URL.revokeObjectURL(this.stylePreview);
        this.stylePhoto = file; this.stylePreview = URL.createObjectURL(file); this.styleConsent = false;
      },
      async submitStyle() {
        if (this.styleBusy) return;
        if (this.styleMode !== 'text' && (!this.stylePhoto || !this.styleConsent)) {
          this.$message.warning('请上传照片并同意本机处理'); return;
        }
        this.styleBusy = true; this.styleComparison = null; this.styleKeepIds = [];
        const generation = ++this.styleRequestGeneration;
        clearTimeout(this.styleTimer);
        try {
          let body = { options: this.styleOptions };
          if (this.styleMode !== 'text') {
            body = new FormData(); body.append('photo', this.stylePhoto);
            body.append('mode', this.styleMode); body.append('consent', 'true');
            body.append('options', JSON.stringify(this.styleOptions));
          }
          this.styleJob = await this.styleRequest('/api/styling/jobs', 'POST', body);
          await this.pollStyleJob(generation);
        } catch (error) { this.styleBusy = false; this.$message.error(error.message); }
      },
      async pollStyleJob(generation) {
        if (!this.styleJob || this.stylePolling || generation !== this.styleRequestGeneration) return;
        this.stylePolling = true;
        try {
          const job = await this.styleRequest('/api/styling/jobs/' + this.styleJob.job_id);
          if (generation !== this.styleRequestGeneration) return;
          this.styleJob = job;
          if (job.status === 'queued' || job.status === 'running') {
            this.styleTimer = setTimeout(() => this.pollStyleJob(generation), 2500);
          } else {
            this.styleBusy = false;
            if (job.status === 'failed') this.$message.error(job.message);
          }
        } catch (error) { this.styleBusy = false; this.$message.error(error.message); }
        finally { this.stylePolling = false; }
      },
      toggleStyleKeep(id) {
        if (this.styleBusy) return;
        this.styleKeepIds = this.styleKeepIds.includes(id) ? this.styleKeepIds.filter(value => value !== id) : this.styleKeepIds.concat(id).slice(-3);
      },
      async refineStyle(feedback) {
        if (this.styleBusy || !this.styleResult) return;
        const text = typeof feedback === 'string' ? feedback : this.styleFeedback;
        if (!text && !this.styleKeepIds.length) { this.$message.info('说说想怎么调整，或点击保留单品'); return; }
        this.styleBusy = true;
        try {
          this.styleJob = await this.styleRequest('/api/styling/jobs/' + this.styleJob.job_id + '/refine', 'POST',
            { feedback: text, keep_product_ids: this.styleKeepIds, options: this.styleOptions });
          this.styleOptions = Object.assign({}, this.styleOptions, this.styleResult.options);
          this.styleFeedback = '';
        } catch (error) { this.$message.error(error.message); }
        finally { this.styleBusy = false; }
      },
      async saveStylePreferences() {
        if (!this.styleSavingConsent) { this.$message.warning('请先勾选同意记住偏好'); return; }
        try {
          await this.styleRequest('/api/styling/preferences', 'POST', { preferences: this.styleOptions, consent: true });
          this.$message.success('已记住风格偏好，照片与身体数据未保存');
        } catch (error) { this.$message.error(error.message); }
      },
      async forgetStylePreferences() {
        try {
          await this.styleRequest('/api/styling/preferences', 'DELETE'); this.styleSavingConsent = false;
          this.$message.success('已删除本会话的长期穿搭偏好');
        } catch (error) { this.$message.error(error.message); }
      },
      async clearStyleAdvice() {
        if (this.styleBusy) return;
        try {
          if (this.styleJob) await this.styleRequest('/api/styling/jobs/' + this.styleJob.job_id, 'DELETE');
          this.styleRequestGeneration++; clearTimeout(this.styleTimer);
          this.styleJob = null; this.styleComparison = null; this.styleKeepIds = [];
          if (this.stylePreview) URL.revokeObjectURL(this.stylePreview);
          this.stylePreview = ''; this.stylePhoto = null; this.styleConsent = false;
        } catch (error) { this.$message.error(error.message); }
      },
      toggleStyleCompare(product) {
        const id = product.product_id;
        if (this.styleCompareIds.includes(id)) this.styleCompareIds = this.styleCompareIds.filter(value => value !== id);
        else if (this.styleCompareIds.length < 3) this.styleCompareIds.push(id);
        else this.$message.info('最多比较三件，请先取消一件');
      },
      async openStyleProduct(card, action) {
        try {
          const response = await fetch(this.base_url + '/api/store/products/' + encodeURIComponent(card.product_id));
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error(data.message || '无法读取商品');
          const product = this.normalizeStoreProduct(data.data);
          if (action === 'size') this.openSizeAdvisor(product);
          else if (action === 'order') {
            if (!this.canDemoOrderProduct(product)) { this.$message.info('当前商品库存尚未核验或缺货，请先咨询客服'); return; }
            this.openOrderPreview(product);
          }
        } catch (error) { this.$message.error(error.message); }
      },
      async compareStyleProducts() {
        if (this.styleCompareIds.length < 2) { this.$message.info('先在商品卡勾选两到三件商品'); return; }
        try {
          this.styleComparison = await this.styleRequest('/api/styling/compare', 'POST', { product_ids: this.styleCompareIds, options: this.styleOptions });
          this.switchStoreTab('styling');
        } catch (error) { this.$message.error(error.message); }
      },
      async tryStyleProduct(card) {
        if (!card || !card.tryon_supported) {
          this.$message.info('演示商品未配置可用于试穿的服装图片；替换为已授权的商品图后即可体验');
          return;
        }
        try {
          const response = await fetch(this.base_url + '/api/store/products/' + encodeURIComponent(card.product_id));
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error(data.message || '无法读取商品');
          this.openTryOn(this.normalizeStoreProduct(data.data));
          if (this.styleMode === 'person' && this.stylePhoto) this.setTryOnPhoto(this.stylePhoto);
          else this.$message.info('试衣需要本人照片，请在试衣窗口选择全身照');
        } catch (error) { this.$message.error(error.message); }
      }
    }
  };
})();
