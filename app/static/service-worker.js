const CACHE='ocpp-pwa-v74';
const STATIC=['/static/branding/voltcore-community-app-icon.svg','/static/branding/voltcore-community-app-icon.svg'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(c=>c.addAll(STATIC)));self.skipWaiting()});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim()});
self.addEventListener('fetch',event=>{
  const req=event.request;
  if(req.method!=='GET')return;
  const url=new URL(req.url);
  if(url.origin!==location.origin)return;
  // Authenticated pages, APIs and public account data always remain network-only.
  if(url.pathname.startsWith('/api/')||url.pathname.startsWith('/public/')||req.mode==='navigate')return;
  if(STATIC.includes(url.pathname))event.respondWith(caches.match(req).then(hit=>hit||fetch(req)));
});

self.addEventListener('push',event=>{
  let data={};
  try{data=event.data?event.data.json():{}}catch(_){data={title:'VoltCore',body:event.data?event.data.text():''}}
  const title=data.title||data.product||'VoltCore';
  const options={
    body:data.body||'',
    icon:data.icon||'/static/branding/voltcore-community-app-icon.svg',
    badge:data.badge||'/static/branding/voltcore-community-app-icon.svg',
    tag:data.tag||'voltcore-notification',
    renotify:data.severity==='critical'||data.severity==='warning',
    data:{url:data.url||'/'}
  };
  event.waitUntil(self.registration.showNotification(title,options));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=new URL(event.notification?.data?.url||'/',self.location.origin).href;
  event.waitUntil((async()=>{
    const clientsList=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of clientsList){
      try{
        if('navigate' in client)await client.navigate(target);
        return client.focus();
      }catch(_){}
    }
    return self.clients.openWindow?self.clients.openWindow(target):undefined;
  })());
});
