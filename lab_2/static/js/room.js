// Синхронизация комнаты совместного просмотра через REST API
const roomCode = document.getElementById('room-data').dataset.code;
const isHost = document.getElementById('room-data').dataset.isHost === "True";

let localTime = 0.0;
let localPlaying = false;

async function syncState() {
    try {
        const res = await fetch(`/api/room/${roomCode}/sync`);
        const data = await res.json();

        localTime = data.current_time_sec;
        localPlaying = data.is_playing;

        // Обновляем плеер
        document.getElementById('screen-icon').innerText = localPlaying ? "▶️ Воспроизведение..." : "⏸️ Пауза";
        const btn = document.getElementById('host-play-btn');
        if (btn) btn.innerText = localPlaying ? "⏸ Пауза" : "▶ Воспроизведение";

        const mins = Math.floor(localTime / 60);
        const secs = Math.floor(localTime % 60);
        document.getElementById('timeline').innerText =
            `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;

        // Обновляем список участников
        document.getElementById('member-count').innerText = data.members.length;
        document.getElementById('member-list').innerText = data.members.join(', ');

        // Обновляем чат
        const chatBox = document.getElementById('chat-messages');
        chatBox.innerHTML = data.chat.map(m =>
            `<div class="chat-msg"><b>${m.sender}</b> <span style="font-size:10px; color:#64748b;">[${m.timestamp}]</span>: ${m.text}</div>`
        ).join('');
        chatBox.scrollTop = chatBox.scrollHeight;

    } catch (e) {
        console.error("Ошибка синхронизации:", e);
    }
}

async function hostControl(action) {
    if (!isHost) return;
    let newPlay = localPlaying;
    let newTime = localTime;

    if (action === 'toggle') newPlay = !localPlaying;
    if (action === 'back') newTime = Math.max(0, localTime - 10);
    if (action === 'fwd') newTime = localTime + 10;

    await fetch(`/api/room/${roomCode}/playback`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ is_playing: newPlay, time_sec: newTime })
    });
    syncState();
}

async function sendChat() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    await fetch(`/api/room/${roomCode}/chat`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ text: text })
    });
    input.value = '';
    syncState();
}

setInterval(syncState, 1500);
syncState();