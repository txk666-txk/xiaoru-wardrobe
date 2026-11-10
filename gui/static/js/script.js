// 定义变量
const slideDistance = 500;
let currentTranslate = 0;
let menu, prevButton, nextButton;
let menuObserver;
let updateScheduled = false;

function updateButtons() {
    if (!prevButton || !nextButton || !menu) return;
    const maxTranslate = Math.max(menu.scrollWidth - menu.clientWidth, 0);
    currentTranslate = Math.max(-maxTranslate, Math.min(currentTranslate, 0));
    menu.style.transform = `translateX(${currentTranslate}px)`;
    prevButton.disabled = currentTranslate >= 0;
    nextButton.disabled = maxTranslate === 0 || currentTranslate <= -maxTranslate;
}

function scheduleButtonUpdate() {
    if (updateScheduled) return;
    updateScheduled = true;
    const schedule = window.requestAnimationFrame || function (callback) { return setTimeout(callback, 0); };
    schedule(() => {
        updateScheduled = false;
        updateButtons();
    });
}

window.refreshUserSlider = scheduleButtonUpdate;

function initializeSlider() {
    // 获取DOM元素
    menu = document.querySelector('.menu');
    prevButton = document.getElementById('prevButton');
    nextButton = document.getElementById('nextButton');
    
    // 确保所有元素都存在
    if (!menu || !prevButton || !nextButton) return;

    if (menuObserver) menuObserver.disconnect();
    menuObserver = new MutationObserver(scheduleButtonUpdate);
    menuObserver.observe(menu, { childList: true, subtree: true });
    window.addEventListener('resize', scheduleButtonUpdate);
    
    // 添加事件监听
    prevButton.addEventListener('click', () => {
        if (menu.scrollWidth > menu.clientWidth) { 
            currentTranslate = Math.min(currentTranslate + slideDistance, 0); 
            menu.style.transform = `translateX(${currentTranslate}px)`;
            updateButtons();
        }
    });
    
    nextButton.addEventListener('click', () => {
        if (menu.scrollWidth > menu.clientWidth) { 
            currentTranslate = Math.max(currentTranslate - slideDistance, -(menu.scrollWidth - menu.clientWidth)); 
            menu.style.transform = `translateX(${currentTranslate}px)`;
            updateButtons();
        }
    });
    
    // 初始化按钮状态
    scheduleButtonUpdate();
}

// 在DOM加载完成后初始化
document.addEventListener('DOMContentLoaded', initializeSlider);
