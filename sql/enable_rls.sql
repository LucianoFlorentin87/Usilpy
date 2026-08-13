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
-- Ejecutar en: Supabase → SQL Editor → pegar todo y correr (Run).

-- Recorre TODAS las tablas del esquema public y les activa RLS.
-- Así no falla si alguna tabla no existe o si se agregan nuevas.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t.tablename);
        RAISE NOTICE 'RLS activado en: %', t.tablename;
    END LOOP;
END $$;

-- Sin políticas (POLICY) definidas, RLS deniega todo a los roles anon y
-- authenticated. Es exactamente lo que queremos: nadie entra por la API REST.

-- Verificación: todas deben quedar con rls_activado = true
SELECT tablename AS tabla, rowsecurity AS rls_activado
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;
