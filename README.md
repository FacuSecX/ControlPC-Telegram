# 🖥️ PC Control Telegram Bot

<p align="center">
<img src="http://imgfz.com/i/loyaHck.png" title="Pccontroltelegram">
</p>
<br></br>

<p align="center">
<a href="https://github.com/FacuSecX"><img title="Autor" src="https://img.shields.io/badge/Author-Facu%20-blue?style=for-the-badge&logo=github"></a>
<a href=""><img title="Version" src="https://img.shields.io/badge/Version-1.0-red?style=for-the-badge&logo="></a>
</p>

<p align="center">
<a href=""><img title="System" src="https://img.shields.io/badge/Supported%20OS-Linux-orange?style=for-the-badge&logo=linux"></a>
<a href="https://paypal.me/FacuSecX"><img title="Paypal" src="https://img.shields.io/badge/Donate-PayPal-green.svg?style=for-the-badge&logo=paypal"></a>
</p>

<p align="center">
<a href="mailto:facusex@gmail.com"><img title="Correo" src="https://img.shields.io/badge/Correo-facusecX@gmail.com-blueviolet?style=for-the-badge&logo=gmai"></a>
<a href="https://t.me/FacuSecX"><img title="Chat" src="https://img.shields.io/badge/CHAT-TELEGRAM-blue?style=for-thjlje-badge&logo=telegram"></a>


Bot de **Telegram desarrollado en Python** para controlar y supervisar de forma remota una PC con Windows 10/11.

Permite acceder a distintas funciones del sistema directamente desde un menú interactivo de Telegram, con acceso restringido al usuario autorizado.


---

## ✨ Funciones

* 📊 Estado del sistema: CPU, RAM, GPU, disco y red
* 📸 Capturas de pantalla 
* 📸 Apagar o encender un monitor
* 🎵 Control remoto de VLC y playlists
* 🔊 Control de volumen y dispositivos de audio
* 🖥️ Gestión de monitores
* ⚙️ Visualización y control de procesos
* 🪟 Gestión de ventanas abiertas
* 📁 Explorador y transferencia de archivos
* 📋 Control del portapapeles
* ⌨️ Ejecución remota de comandos CMD
* 🔔 Notificaciones de Windows
* ⚡ Controles rápidos de teclado y multimedia
* 🔌 Bloquear, suspender, reiniciar o apagar el PC
* ⭐ Macros personalizables
* 🔐 Modo administrador temporal
* 🤖 Estado y reinicio remoto del bot

---

## 🔒 Seguridad

El acceso al bot está restringido mediante el **Telegram User ID** autorizado.

Las credenciales y datos privados se configuran mediante variables de entorno y no deben almacenarse directamente en el código.

```env
TELEGRAM_BOT_TOKEN=TU_TOKEN
ALLOWED_USER_ID=TU_USER_ID
VLC_PASSWORD=TU_PASSWORD
ADMIN_PIN=
```


El Admin PIN permite proteger acceder al modo administrador mediante una contraseña... los valores VLC PATH SOUNDVOLUMEVIEW_PATH y SETVOL_PATH para control de volumen
modifican la ruta donde tengan el .exe


---
## ⚙️ Requisitos

* Windows 10 / 11
* Python 3
* Bot de Telegram
* VLC *(para las funciones multimedia)*
* SoundVolumeView / SetVol *( opcional para las funciones avanzadas de audio)*

Instala las dependencias necesarias y configura las variables de entorno antes de iniciar el bot.

```bash
python bot.py
```

---

## 📱 Uso

Una vez iniciado, el bot permite controlar el PC remotamente  desde su **panel principal de Telegram**.

La mayoría de las funciones se manejan mediante botones interactivos, sin necesidad de escribir comandos manualmente, pueden adaptar el codigo
y añadirle comandos personalizados dependiendo sus funciones de uso. pueden agregar el proceso y añadirlo que se ejecuta apenas arranque windows para no tener
que ejecutar el bot manualmente cada vez que enciendan el PC.

---

## ⚠️ Aviso

Este proyecto proporciona funciones de **control remoto del sistema**, incluyendo ejecución de comandos, administración de archivos y control de energía.

Está diseñado exclusivamente para administrar **equipos propios o sistemas donde tengas autorización**.


