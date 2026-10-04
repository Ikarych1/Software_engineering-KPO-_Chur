// Автономный плеер для страницы фильма
let isPlaying = false;
let currentTime = 0;
let timerInterval = null;

function togglePlay() {
    isPlaying = !isPlaying;
    const btn = document.getElementById('play-btn');
    const icon = document.getElementById('screen-icon');

    if (isPlaying) {
        btn.innerText = "⏸ Пауза";
        icon.innerText = "▶️";
        timerInterval = setInterval(() => {
            currentTime += 1;
            renderTime();
        }, 1000);
    } else {
        btn.innerText = "▶ Воспроизведение";
        icon.innerText = "⏸️";
        clearInterval(timerInterval);
    }
}

function seek(seconds) {
    currentTime = Math.max(0, currentTime + seconds);
    renderTime();
}

function renderTime() {
    const mins = Math.floor(currentTime / 60);
    const secs = Math.floor(currentTime % 60);
    document.getElementById('timeline').innerText =
        `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}