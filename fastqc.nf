// Default parameters
params.nogroup = false
params.min_length = 0
params.kmers = 7

process fastqc {
    container 'quay.io/biocontainers/fastqc:0.12.1'

    input:
    path input_file
    path contaminants
    path adapters
    path limits
    val nogroup
    val min_length
    val kmers

    output:
    path "*.html", emit: html_file
    path "*.txt", emit: text_file

    script:
    """
    #import re
        #set input_name = re.sub('[^\w\-\s]', '_', str($input_file.element_identifier))

        #if $input_file.ext.endswith('.gz'):
            #set input_file_sl = $input_name + '.gz'
        #elif $input_file.ext.endswith('.bz2'):
            #set input_file_sl = $input_name + '.bz2'
        #else
            #set input_file_sl = $input_name
        #end if

        #if 'bam' in $input_file.ext:
            #set format = 'bam'
        #elif 'sam' in $input_file.ext:
            #set format = 'sam'
        #else
            #set format = 'fastq'
        #end if

        ln -s '${input_file}' '${input_file_sl}' &&
        mkdir -p '${html_file.files_path}' &&
        fastqc
            --outdir '${html_file.files_path}'
            #if $contaminants.dataset and str($contaminants) > ''
                --contaminants '${contaminants}'
            #end if

            #if $adapters.dataset and str($adapters) > ''
                --adapters '${adapters}'
            #end if

            #if $limits.dataset and str($limits) > ''
                --limits '${limits}'
            #end if
            --threads \${GALAXY_SLOTS:-2}
            --dir \${TEMP:-\$_GALAXY_JOB_TMP_DIR}
            --quiet
            --extract
            #if $min_length:
                --min_length $min_length
            #end if
            $nogroup
            --kmers $kmers
            -f '${format}'
            '${input_file_sl}'

        && cp '${html_file.files_path}'/*/fastqc_data.txt output.txt
        && cp '${html_file.files_path}'/*\.html output.html
    """
}