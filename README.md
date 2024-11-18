# multi-push
pushing files via SFTP using multiple streams per file

This is an experimental project to explore supporting multi-stream "push" of large files from a client to a server by breaking the source file into chunks and then uploading those chunks in parallel into the proper region within a file on the target machine.  Due to support for uploading to a specific ofset within a file (introduced in OpenSSH x.x), this should be possible. 

Consider this a proof of concept and it sort of works, but sometimes truncates or otherwise corrupts the file on the remote side. I suspect this has to do with exceeding some threshold regarding th enumbert of concurrent sessions on the server side, though that has yet to be proven. It is not currently tracked, nor is a failed connection retried. 

Example command line
```
./multi-push-sftp.py  --remote_host lvn-catalog --directory_path /hol/lib/cert-export01  --username catalog-mgr --remote_directory /hol/lib/push_test
```

It uses key-based SSH, which must be setup in advance. 

November 18, 2024