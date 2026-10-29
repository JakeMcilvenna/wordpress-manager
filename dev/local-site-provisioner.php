<?php
// Fixed local-Docker-only installation broker. Never deploy this to Hetzner.
// Single dedicated WordPress volume + dedicated database; no Docker socket,
// shell, root, host mounts, runtime package installation or arbitrary commands.
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

function reply(int $code, array $data): never {
    http_response_code($code);
    echo json_encode($data, JSON_THROW_ON_ERROR);
    exit;
}

$secret = getenv('WM_DEV_BROKER_TOKEN') ?: '';
$provided = $_SERVER['HTTP_X_WM_LOCAL_TOKEN'] ?? '';
if (strlen($secret) < 24 || !is_string($provided) || !hash_equals($secret, $provided)) {
    reply(403, ['error' => 'Forbidden']);
}

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$method = $_SERVER['REQUEST_METHOD'] ?? '';
if (!in_array($path, ['/status', '/install'], true)) {
    reply(404, ['error' => 'Unknown route']);
}
if (($path === '/status' && $method !== 'GET') ||
    ($path === '/install' && $method !== 'POST')) {
    reply(405, ['error' => 'Wrong method']);
}

function wp_cli(array $arguments, ?string $stdin = null): bool|string {
    // Every argument is a separate argv token; no shell or user-provided path.
    $argv = array_merge(['wp'], $arguments, [
        '--path=/var/www/html', '--skip-plugins', '--skip-themes',
        '--skip-packages', '--no-color',
    ]);
    $process = proc_open($argv, [
        0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w'],
    ], $pipes, '/var/www/html');
    if (!is_resource($process)) {
        return false;
    }
    if ($stdin !== null) {
        fwrite($pipes[0], $stdin . "\n");
    }
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1], 4096);
    fclose($pipes[1]);
    // Do not emit WordPress/PHP errors or credentials in HTTP responses.
    stream_get_contents($pipes[2], 4096);
    fclose($pipes[2]);
    $exit = proc_close($process);
    return $exit === 0 ? trim((string) $out) : false;
}

function installed(): bool {
    return wp_cli(['core', 'is-installed', '--quiet']) !== false;
}

$ready = is_file('/var/www/html/wp-config.php')
    && is_file('/var/www/html/wp-includes/version.php');

if ($path === '/status') {
    if (!$ready) {
        reply(200, ['ready' => false, 'installed' => false]);
    }
    if (!installed()) {
        reply(200, ['ready' => true, 'installed' => false]);
    }
    $title = wp_cli(['option', 'get', 'blogname', '--quiet']);
    $version = wp_cli(['core', 'version', '--quiet']);
    reply(200, [
        'ready' => true, 'installed' => true,
        'title' => is_string($title) ? mb_substr($title, 0, 70) : 'Second local site',
        'version' => is_string($version) ? $version : '',
    ]);
}

if (!$ready) {
    reply(503, ['error' => 'WordPress files are still being prepared']);
}
if (installed()) {
    reply(409, ['error' => 'Already installed']);
}
$length = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
if ($length <= 0 || $length > 1024) {
    reply(400, ['error' => 'Invalid request size']);
}
$input = json_decode(file_get_contents('php://input', false, null, 0, 1025), true);
if (!is_array($input)) {
    reply(400, ['error' => 'Invalid JSON']);
}
$title = $input['title'] ?? null;
$email = $input['email'] ?? null;
if (!is_string($title) || strlen($title) < 1 || strlen($title) > 70 ||
    trim($title) !== $title || preg_match('/[\x00-\x1f\x7f]/', $title)) {
    reply(400, ['error' => 'Invalid title']);
}
if (!is_string($email) || strlen($email) > 120 ||
    !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    reply(400, ['error' => 'Invalid email']);
}

// Only the already-provisioned dedicated localhost port and fixed admin user
// can be installed. Admin password travels over stdin, not CLI argv, shell
// history, process listings or image environment. Returned once over loopback.
$password = bin2hex(random_bytes(18));
$result = wp_cli([
    'core', 'install', '--url=http://127.0.0.1:8092', '--title=' . $title,
    '--admin_user=admin', '--admin_email=' . $email,
    '--skip-email', '--prompt=admin_password', '--quiet',
], $password);
if ($result === false) {
    reply(503, ['error' => 'Installation failed. Inspect the local provisioner container.']);
}
reply(200, [
    'installed' => true, 'url' => 'http://127.0.0.1:8092/',
    'admin_url' => 'http://127.0.0.1:8092/wp-admin/',
    'username' => 'admin', 'password' => $password,
]);
