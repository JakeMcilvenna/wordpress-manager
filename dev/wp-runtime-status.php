<?php
// Docker development only; never mount or deploy this file on a public server.
// Avoid bootstrapping WordPress or querying its database (no credentials read).
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
$version_file = '/var/www/html/wp-includes/version.php';
if (!is_file($version_file)) {
    http_response_code(503);
    echo json_encode(['error' => 'WordPress files are not installed']);
    exit;
}
require $version_file;
echo json_encode([
    'php_version' => PHP_VERSION,
    'wordpress_version' => $wp_version,
], JSON_THROW_ON_ERROR);
