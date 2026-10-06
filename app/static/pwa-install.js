(()=>{
  let deferred=null;
  const buttons=()=>[...document.querySelectorAll('[data-pwa-install]')];
  const standalone=()=>window.matchMedia?.('(display-mode: standalone)').matches||window.navigator.standalone===true;
  const isiOS=()=>/iphone|ipad|ipod/i.test(navigator.userAgent||'');
  const syncInstall=()=>{const installed=standalone();buttons().forEach(b=>{b.hidden=installed||(!deferred&&!isiOS());if(!b.hidden)b.textContent=isiOS()&&!deferred?'▣ Zum Home-Bildschirm':'▣ App installieren'})};
  window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferred=e;syncInstall()});
  window.addEventListener('appinstalled',()=>{deferred=null;syncInstall()});
  document.addEventListener('click',async e=>{const b=e.target.closest('[data-pwa-install]');if(!b)return;if(deferred){const prompt=deferred;deferred=null;await prompt.prompt();try{await prompt.userChoice}catch(_){ }syncInstall();return}if(isiOS())alert('Auf iPhone/iPad: Teilen öffnen und „Zum Home-Bildschirm“ wählen. Danach startet die Oberfläche wie eine App.')});
  if('serviceWorker' in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/service-worker.js').catch(()=>{}));
  document.addEventListener('DOMContentLoaded',syncInstall);syncInstall();

  const pushPanel=document.getElementById('pushNotificationPanel');
  if(!pushPanel)return;
  const status=document.getElementById('pushNotificationStatus');
  const toggle=document.getElementById('pushNotificationToggle');
  const test=document.getElementById('pushNotificationTest');
  const severity=document.getElementById('pushNotificationSeverity');
  let currentSubscription=null;
  let publicKey='';
  let busy=false;

  const supported=()=>('serviceWorker' in navigator)&&('PushManager' in window)&&('Notification' in window);
  const b64ToBytes=value=>{
    const padding='='.repeat((4-value.length%4)%4);
    const base64=(value+padding).replace(/-/g,'+').replace(/_/g,'/');
    const raw=atob(base64);
    return Uint8Array.from([...raw].map(ch=>ch.charCodeAt(0)));
  };
  const setBusy=value=>{busy=!!value;toggle.disabled=busy||toggle.dataset.blocked==='1';severity.disabled=busy||!currentSubscription;test.disabled=busy||!currentSubscription};
  const setMessage=(text,kind='')=>{status.textContent=text;status.className='account-push-status'+(kind?' '+kind:'')};

  async function registration(){
    await navigator.serviceWorker.register('/service-worker.js');
    return navigator.serviceWorker.ready;
  }
  async function config(){
    const r=await fetch('/api/notifications/push/config',{cache:'no-store'});
    if(!r.ok)throw new Error('Push-Konfiguration konnte nicht geladen werden');
    return r.json();
  }
  async function serverSubscribe(sub){
    const json=sub.toJSON();
    const r=await fetch('/api/notifications/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:json.endpoint,keys:json.keys||{},min_severity:severity.value})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(d.detail||'Push konnte nicht aktiviert werden');
    return d;
  }
  async function syncPush(){
    if(!supported()){
      toggle.dataset.blocked='1';toggle.disabled=true;severity.disabled=true;test.disabled=true;
      setMessage('Dieser Browser unterstützt Web-Push nicht.','muted');
      return;
    }
    if(!window.isSecureContext){
      toggle.dataset.blocked='1';toggle.disabled=true;severity.disabled=true;test.disabled=true;
      setMessage('Push benötigt eine sichere HTTPS-Verbindung.','warn');
      return;
    }
    try{
      const [reg,cfg]=await Promise.all([registration(),config()]);
      publicKey=cfg.public_key||'';
      let sub=await reg.pushManager.getSubscription();
      const known=(cfg.subscriptions||[]).find(x=>sub&&x.endpoint===sub.endpoint);
      if(sub&&!known){
        // A browser subscription from another/login-old account must never receive
        // messages for that account while a different account is active.
        try{await sub.unsubscribe()}catch(_){}
        sub=null;
      }
      currentSubscription=sub;
      if(sub&&known){
        severity.value=known.min_severity||'warning';
        toggle.textContent='Push deaktivieren';
        toggle.classList.remove('primary');toggle.classList.add('secondary');
        test.hidden=false;test.disabled=false;severity.disabled=false;
        setMessage(Notification.permission==='granted'?'Push ist für dieses Gerät aktiv.':'Push-Abo vorhanden, Browserberechtigung prüfen.','success');
      }else{
        toggle.textContent='Push aktivieren';
        toggle.classList.add('primary');toggle.classList.remove('secondary');
        test.hidden=true;severity.disabled=true;
        if(Notification.permission==='denied'){
          toggle.dataset.blocked='1';toggle.disabled=true;
          setMessage('Benachrichtigungen sind im Browser blockiert. Bitte dort wieder erlauben.','warn');
        }else{
          toggle.dataset.blocked='0';toggle.disabled=false;
          setMessage('Push ist auf diesem Gerät noch nicht aktiviert.','muted');
        }
      }
    }catch(e){
      console.error(e);setMessage(e.message||'Push-Status konnte nicht geladen werden.','error');
    }
  }
  toggle.addEventListener('click',async()=>{
    if(busy||toggle.dataset.blocked==='1')return;
    setBusy(true);
    try{
      const reg=await registration();
      let sub=await reg.pushManager.getSubscription();
      if(sub){
        try{await fetch('/api/notifications/push/subscribe',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:sub.endpoint})})}catch(_){}
        await sub.unsubscribe();
        currentSubscription=null;
      }else{
        let permission=Notification.permission;
        if(permission!=='granted')permission=await Notification.requestPermission();
        if(permission!=='granted')throw new Error(permission==='denied'?'Benachrichtigungen wurden im Browser blockiert.':'Benachrichtigungen wurden nicht erlaubt.');
        if(!publicKey){const cfg=await config();publicKey=cfg.public_key||''}
        if(!publicKey)throw new Error('Push-Schlüssel ist nicht verfügbar.');
        sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64ToBytes(publicKey)});
        try{await serverSubscribe(sub)}catch(e){try{await sub.unsubscribe()}catch(_){}throw e}
        currentSubscription=sub;
      }
      await syncPush();
    }catch(e){
      console.error(e);setMessage(e.message||'Push konnte nicht geändert werden.','error');
    }finally{setBusy(false)}
  });
  severity.addEventListener('change',async()=>{
    if(!currentSubscription||busy)return;
    setBusy(true);
    try{
      const r=await fetch('/api/notifications/push/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:currentSubscription.endpoint,min_severity:severity.value})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||'Push-Einstellung konnte nicht gespeichert werden');
      setMessage('Push-Einstellung gespeichert.','success');
    }catch(e){setMessage(e.message||'Push-Einstellung konnte nicht gespeichert werden.','error')}
    finally{setBusy(false)}
  });
  test.addEventListener('click',async()=>{
    if(!currentSubscription||busy)return;
    setBusy(true);
    try{
      const r=await fetch('/api/notifications/push/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:currentSubscription.endpoint})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(d.detail||'Test-Push fehlgeschlagen');
      setMessage('Test-Push wurde versendet.','success');
    }catch(e){setMessage(e.message||'Test-Push fehlgeschlagen.','error')}
    finally{setBusy(false)}
  });
  document.addEventListener('DOMContentLoaded',syncPush);
  syncPush();
})();
