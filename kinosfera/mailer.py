"""Outgoing email: SMTP when configured, otherwise .eml files in .local/outbox for local development."""
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import logging
import os
from pathlib import Path
import smtplib
import ssl
import threading
import time

from flask import current_app, url_for

log = logging.getLogger('kinosfera.mail')


def settings():
    env = os.environ.get
    port = int(env('CINEMA_SMTP_PORT') or 587)
    return dict(host=env('CINEMA_SMTP_HOST', ''), port=port,
                user=env('CINEMA_SMTP_USER', ''), password=env('CINEMA_SMTP_PASSWORD', ''),
                sender=env('CINEMA_SMTP_FROM') or env('CINEMA_SMTP_USER') or 'КиноСфера <no-reply@kinosfera.local>',
                security=(env('CINEMA_SMTP_SECURITY') or ('ssl' if port == 465 else 'starttls')).lower())


def public_url(endpoint, **values):
    """Absolute link for emails. CINEMA_PUBLIC_URL protects links from a forged Host header."""
    base = os.environ.get('CINEMA_PUBLIC_URL', '').rstrip('/')
    return base + url_for(endpoint, **values) if base else url_for(endpoint, _external=True, **values)


def build(to, subject, text, html=None, sender=None):
    message = EmailMessage()
    message['From'] = sender or settings()['sender']
    message['To'] = to
    message['Subject'] = subject
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid(domain='kinosfera.local')
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype='html')
    return message


def _deliver(message, config, outbox):
    try:
        if not config['host']:
            outbox.mkdir(parents=True, exist_ok=True)
            path = outbox / f'{time.strftime("%Y%m%d-%H%M%S")}-{os.urandom(3).hex()}.eml'
            path.write_bytes(bytes(message))
            log.warning('SMTP не настроен (CINEMA_SMTP_HOST): письмо для %s сохранено в %s', message['To'], path)
            return
        context = ssl.create_default_context()
        if config['security'] == 'ssl':
            client = smtplib.SMTP_SSL(config['host'], config['port'], timeout=20, context=context)
        else:
            client = smtplib.SMTP(config['host'], config['port'], timeout=20)
        with client:
            if config['security'] == 'starttls':
                client.starttls(context=context)
            if config['user']:
                client.login(config['user'], config['password'])
            client.send_message(message)
        log.info('Письмо «%s» отправлено на %s', message['Subject'], message['To'])
    except Exception:
        log.exception('Не удалось отправить письмо на %s', message['To'])


def send(to, subject, text, html=None):
    """Send in the background so the response time does not reveal whether the account exists."""
    config = settings()
    message = build(to, subject, text, html, config['sender'])
    if current_app.testing:
        current_app.extensions.setdefault('mail_outbox', []).append(message)
        return
    outbox = Path(current_app.root_path) / '.local/outbox'
    threading.Thread(target=_deliver, args=(message, config, outbox), name='mail', daemon=True).start()
