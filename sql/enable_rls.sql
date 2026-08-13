-- Cierra el acceso público vía la API REST (PostgREST) de Supabase.
--
-- Contexto: Supabase expone automáticamente las tablas del esquema `public`
-- a través de https://<ref>.supabase.co/rest/v1/<tabla>. Si RLS está
-- deshabilitado, cualquiera con la clave `anon` (que NO es secreta: está
-- pensada para ir en clientes) puede leer y escribir esas tablas.
--
-- Nuestro backend se conecta directo por asyncpg con el rol `postgres`, que es
-- el DUEÑO de las tablas. Los dueños omiten RLS por defecto, así que habilitar
-- RLS NO afecta en nada al funcionamiento del sistema: solo bloquea el acceso
-- anónimo por la API REST.
--
-- Ejecutar en: Supabase → SQL Editor → pegar y correr.

ALTER TABLE public.system_users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.historial_academico   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.correlativas          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cursos                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ejecuciones           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_enrollments   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.course_aliases        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.course_unresolved     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_alumnos          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_asistencias      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_calificaciones   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_cursos           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_log              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_matriculaciones  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_usuarios_365     ENABLE ROW LEVEL SECURITY;

-- Sin políticas (POLICY) definidas, RLS deniega todo a los roles anon y
-- authenticated. Es exactamente lo que queremos: nadie entra por la API REST.

-- Verificación: todas deben quedar en rowsecurity = true
SELECT tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;
