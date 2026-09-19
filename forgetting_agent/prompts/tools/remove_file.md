Remove one workspace file you created with write_file and no longer need, such as a temporary diagnostic script.
- Pre-existing files, directories and symlinks are refused. This is not a general project-file deletion tool.
- A successful write or edit supplies the current observation. If the file changed afterwards, read_file first and check that it is still disposable.
- This deletes the workspace file; it does not archive conversation output. Use context_apply for working-context cleanup when available.
